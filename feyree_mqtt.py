#!/usr/bin/env python3
"""Passerelle locale Feyree F-M24 <-> MQTT (TinyTuya, sans cloud).

Un seul thread dialogue avec le chargeur; le callback MQTT ne fait qu'empiler
les commandes. Pas de stratégie de charge automatique dans cette version.
"""

import argparse
import json
import logging
import math
import queue
import re
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import urlopen

import tinytuya
import paho.mqtt.client as mqtt

LOG = logging.getLogger("feyree_mqtt")
STOP = threading.Event()


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def decode(dps):
    """Champs interprétés d'après le mapping F-M24; les DP bruts restent publiés."""
    out = {}
    mapping = {
        "101": ("device_state", lambda v: str(v)),
        "102": ("voltage_v", lambda v: v / 10.0),
        "105": ("current_a", lambda v: v / 10.0),
        "109": ("power_w", lambda v: v * 100.0),  # unité kW, scale=1
        "110": ("temperature_c", lambda v: v / 10.0),
        "112": ("energy_kwh", lambda v: v / 10.0),
        "114": ("set_current_a", int),
        "118": ("delay_hours", int),
        "120": ("charging_time", str),
        "124": ("operation", str),
        "127": ("cp_v", int),
    }
    for dp, (name, convert) in mapping.items():
        if dp in dps:
            try:
                out[name] = convert(dps[dp])
            except (ValueError, TypeError):
                pass
    return out


def translate_command(obj):
    """Valide les commandes avant de les adresser au chargeur."""
    if not isinstance(obj, dict):
        raise ValueError("Commande JSON attendue, sous forme d'objet")
    action = obj.get("action")
    if action == "start":
        return 124, "OpenCharging"
    if action == "stop":
        return 124, "CloseCharging"
    if action == "wait":
        return 124, "WaitOperation"  # pause sans RAZ des compteurs, pas un verrou
    if action in ("current", "delay"):
        value = obj.get("value")
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("La valeur doit être un entier JSON")
        low, high, dp = (8, 16, 114) if action == "current" else (0, 15, 118)
        if not low <= value <= high:
            raise ValueError(f"{action}: valeur autorisée {low}..{high}")
        return dp, value
    if action == "status":
        return None, None
    raise ValueError("Action inconnue (start, stop, wait, current, delay, status)")


def translate_domoticz_command(payload, indices):
    """Traduit une notification domoticz/out en commande Feyree, ou None.

    Ce sont les commandes provenant des dispositifs utilisateur (pas les
    mises à jour domoticz/in). La prévention des boucles MQTT doit rester
    activée dans le matériel MQTT Client Gateway de Domoticz.
    """
    if not isinstance(payload, dict):
        raise ValueError("Notification Domoticz invalide")
    try:
        idx = int(payload["idx"])
    except (KeyError, TypeError, ValueError):
        return None

    setpoint = indices.get("setpoint")
    switch = indices.get("switch")
    if setpoint is not None and idx == int(setpoint):
        # MQTT Domoticz sépare les champs en svalue1, svalue2...
        raw = payload.get("svalue1", payload.get("svalue"))
        try:
            numeric = float(raw)
        except (TypeError, ValueError):
            raise ValueError("Consigne Domoticz absente ou non numérique")
        if not math.isfinite(numeric) or not numeric.is_integer():
            raise ValueError("Consigne courant : entier attendu")
        value = int(numeric)
        # Vérification stricte des limites identique à feyree/set.
        translate_command({"action": "current", "value": value})
        return {"action": "current", "value": value}

    if switch is not None and idx == int(switch):
        state = payload.get("nvalue")
        if str(state) == "1":
            return {"action": "start"}
        if str(state) == "0":
            return {"action": "stop"}
        # Repli pour d'éventuels messages sans nvalue.
        label = str(payload.get("svalue1", payload.get("svalue", ""))).lower()
        if label == "on":
            return {"action": "start"}
        if label == "off":
            return {"action": "stop"}
        raise ValueError("État interrupteur Domoticz inconnu")
    return None


def read_domoticz_energy_wh(base_url, idx):
    """Lit le total existant du compteur Domoticz (sans jamais le réinitialiser).

    Le champ Data de l'API Domoticz est exprimé en kWh pour un
    dispositif Electricity (instant + counter).
    """
    url = base_url.rstrip("/") + "/json.htm?type=command&param=getdevices&rid=" + str(idx)
    with urlopen(url, timeout=3) as response:
        answer = json.load(response)
    results = answer.get("result") or []
    if not results:
        raise ValueError("Compteur Domoticz introuvable (IDX %s)" % idx)
    data = str(results[0].get("Data", ""))
    match = re.fullmatch(r"\s*([0-9]+(?:[.,][0-9]+)?)\s*kWh\s*", data, re.I)
    if not match:
        raise ValueError("Total compteur inattendu : %r (kWh attendu)" % data)
    return float(match.group(1).replace(",", ".")) * 1000.0


class Gateway:
    def __init__(self, config):
        self.cfg = config
        self.root = config.get("topic", "feyree").rstrip("/")
        self.queue = queue.Queue(maxsize=100)
        self.dps = {}
        self.available = False
        self.last_data = 0.0
        self.last_status = None
        self.last_dp120_publish = None
        self.mqtt_connected = False
        self.domoticz = config.get("domoticz") or {}
        self.domo_enabled = bool(self.domoticz.get("enabled", False))
        self.domo_idx = self.domoticz.get("idx") or {}
        self.domo_topic = self.domoticz.get("topic_in", "domoticz/in")
        self.domo_out_topic = self.domoticz.get("topic_out", "domoticz/out")
        self.domo_write_enabled = bool(self.domoticz.get("write_enabled", False))
        self.domo_last = {}
        self.domo_sync_needed = True
        self.energy_wh = None
        self.energy_last_ts = None
        self.energy_last_w = None
        self.last_power_pub = None
        self.energy_retry_at = 0.0
        self.next_command_query = None
        self.client = self._make_mqtt()

    def _make_mqtt(self):
        # Callback API v2 (paho-mqtt 2.x), avec compatibilité 1.x.
        if hasattr(mqtt, "CallbackAPIVersion"):
            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                 client_id=self.cfg.get("mqtt_client_id", "feyree-mqtt"))
        else:
            client = mqtt.Client(client_id=self.cfg.get("mqtt_client_id", "feyree-mqtt"))
        if self.cfg.get("mqtt_username"):
            client.username_pw_set(self.cfg["mqtt_username"], self.cfg.get("mqtt_password"))
        client.will_set(self.root + "/availability", "offline", qos=1, retain=True)
        client.on_connect = self.on_connect
        client.on_disconnect = self.on_disconnect
        client.on_message = self.on_message
        client.reconnect_delay_set(min_delay=1, max_delay=60)
        return client

    def on_connect(self, client, userdata, flags, reason_code, *args):
        if reason_code != 0:
            LOG.error("Connexion MQTT refusée : %s", reason_code)
            return
        self.mqtt_connected = True
        self.domo_sync_needed = True
        client.subscribe(self.root + "/set", qos=1)
        if self.domo_enabled and self.domo_write_enabled:
            client.subscribe(self.domo_out_topic, qos=1)
        # Une reconnexion du broker ne transforme pas un état périmé en état frais.
        self.publish_availability()
        if self.last_status is not None:
            self.publish(self.root + "/state", self.last_status, retain=True)
        LOG.info("Connecté au broker MQTT")

    def on_disconnect(self, client, userdata, flags_or_rc, reason_code=None, *args):
        self.mqtt_connected = False
        LOG.warning("MQTT déconnecté")

    def on_message(self, client, userdata, msg):
        topic = getattr(msg, "topic", self.root + "/set")
        if topic != self.root + "/set" and not (
            self.domo_enabled and self.domo_write_enabled and
            topic == self.domo_out_topic
        ):
            return
        if msg.retain:
            LOG.warning("Commande MQTT retenue ignorée (sécurité) : %s", topic)
            return
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
            if topic == self.domo_out_topic:
                obj = translate_domoticz_command(payload, self.domo_idx)
                if obj is None:
                    return  # les autres dispositifs Domoticz sont ignorés
            else:
                obj = payload
            dp, value = translate_command(obj)
            if not self.available:
                raise ValueError("Chargeur hors ligne : commande non mémorisée")
            self.queue.put_nowait((obj, dp, value))
        except (ValueError, UnicodeDecodeError, queue.Full) as exc:
            self.publish(self.root + "/ack", {"time": utc_now(), "ok": False,
                                                 "error": str(exc)}, retain=False)

    def publish(self, topic, obj, retain=True):
        payload = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False,
                                                              separators=(",", ":"))
        self.client.publish(topic, payload, qos=1, retain=retain)

    def publish_availability(self):
        if self.mqtt_connected:
            self.publish(self.root + "/availability",
                         "online" if self.available else "offline")

    def set_available(self, value):
        if self.available != value:
            self.available = value
            self.publish_availability()
            LOG.info("Chargeur %s", "en ligne" if value else "hors ligne")

    def ingest(self, response):
        """Fusionne les mises à jour partielles, y compris data.dps."""
        if not isinstance(response, dict):
            return False
        if "Error" in response or "Err" in response:
            raise ConnectionError("Réponse TinyTuya en erreur : %s" % response)
        dps = response.get("dps") or (response.get("data") or {}).get("dps")
        if not isinstance(dps, dict) or not dps:
            return False  # ACK ou heartbeat sans DP
        dps = {str(k): v for k, v in dps.items()}
        previous_time = self.dps.get("120")
        changed = {k: v for k, v in dps.items() if k not in self.dps or self.dps[k] != v}
        self.dps.update(dps)
        now = time.monotonic()
        self.last_data = now
        self.set_available(True)
        if self.domo_enabled:
            self.update_domoticz(changed, now)

        # Le chargeur pousse DP120 (Ctime) chaque seconde en charge.
        # On conserve toutes les mesures en mémoire, mais on réduit leur publication.
        # Tout autre changement reste immédiat, y compris les changements du CP.
        to_publish = {dp: value for dp, value in changed.items() if dp != "120"}
        if "120" in changed:
            period = float(self.cfg.get("dp120_publish_seconds", 30))
            def as_seconds(value):
                try:
                    h, m, s = (int(x) for x in str(value).split(":"))
                    return 3600 * h + 60 * m + s
                except (TypeError, ValueError):
                    return None

            previous_seconds = as_seconds(previous_time) if previous_time is not None else None
            current_seconds = as_seconds(self.dps["120"])
            reset = (previous_seconds is not None and current_seconds is not None
                     and current_seconds < previous_seconds)
            if (to_publish or self.last_dp120_publish is None or
                    now - self.last_dp120_publish >= period or reset):
                to_publish["120"] = self.dps["120"]
                self.last_dp120_publish = now

        if to_publish:
            snapshot = {
                "time": utc_now(),
                "dps": dict(self.dps),
                "decoded": decode(self.dps),
            }
            self.last_status = snapshot
            self.publish(self.root + "/state", snapshot)
            for dp, value in to_publish.items():
                self.publish(self.root + "/dp/" + dp, value)
            LOG.debug("DP publiés : %s", to_publish)
        return True


    def publish_domoticz(self, name, nvalue, svalue, *, trigger=False):
        """Mise à jour d'un dispositif existant, identifie par son IDX."""
        idx = self.domo_idx.get(name)
        if not idx or not self.mqtt_connected:
            return
        payload = {"command": "udevice", "idx": int(idx),
                   "nvalue": nvalue, "svalue": str(svalue),
                   "parse": bool(trigger)}
        signature = (nvalue, str(svalue))
        if self.domo_sync_needed or self.domo_last.get(name) != signature:
            self.publish(self.domo_topic, payload, retain=False)
            self.domo_last[name] = signature

    def ensure_energy_seed(self, now):
        """Recupere le compteur deja existant dans Domoticz avant toute ecriture."""
        if not self.domo_idx.get("power") or self.energy_wh is not None:
            return
        if now < self.energy_retry_at:
            return
        self.energy_retry_at = now + 60
        base = self.domoticz.get("http_url")
        if not base:
            LOG.warning("IDX power configure mais domoticz.http_url absent : "
                        "compteur d'energie non modifie")
            return
        try:
            self.energy_wh = read_domoticz_energy_wh(base, self.domo_idx["power"])
            LOG.info("Compteur d'energie Domoticz repris a %.1f Wh", self.energy_wh)
        except Exception as exc:
            # Fail closed : on ne publie jamais 'puissance;0' qui effacerait le total.
            LOG.warning("Impossible de lire l'index d'energie existant : %s", exc)

    def update_domoticz(self, changed, now):
        """Publie les mesures en MQTT /domoticz/in; sans aucune decision de charge."""
        if not self.domo_enabled:
            return
        self.ensure_energy_seed(now)
        dps = self.dps
        force = self.domo_sync_needed
        scalar = (
            ("110", "temperature", lambda v: v / 10.0),
            ("102", "voltage", lambda v: v / 10.0),
            ("105", "current", lambda v: v / 10.0),
            ("101", "state", str),
            ("127", "cp", int),  # Tension Control Pilot mesuree (V)
            ("114", "setpoint", int),
        )
        for dp, name, conversion in scalar:
            if dp in dps and (force or dp in changed):
                value = conversion(dps[dp])
                # L'etat doit generer un evenement Domoticz pour les automatismes.
                # Les synchronisations de consigne ne doivent pas produire de boucles.
                self.publish_domoticz(name, 0, value, trigger=(name in ("state", "cp")))

        if "124" in dps and (force or "124" in changed):
            operation = dps["124"]
            # WaitOperation = pause ; on n'impose pas Off sur un switch On/Off.
            if operation == "OpenCharging":
                self.publish_domoticz("switch", 1, "On")
            elif operation == "CloseCharging":
                self.publish_domoticz("switch", 0, "Off")

        if "109" in dps:
            power_w = max(0.0, float(dps["109"]) * 100.0)
            # Integration avec le temps reel, et NON +P/60 a chaque evenement.
            if self.energy_wh is not None and self.energy_last_ts is not None:
                dt = now - self.energy_last_ts
                if 0 < dt <= 120 and self.energy_last_w is not None:
                    self.energy_wh += self.energy_last_w * dt / 3600.0
            self.energy_last_ts = now
            self.energy_last_w = power_w
            publish_due = (force or "109" in changed or
                           self.last_power_pub is None or
                           now - self.last_power_pub >=
                           float(self.domoticz.get("power_publish_seconds", 60)))
            if self.energy_wh is not None and publish_due:
                self.publish_domoticz("power", 0,
                    "%s;%d" % (format(power_w, "g"), round(self.energy_wh)))
                self.last_power_pub = now
        if self.mqtt_connected:
            self.domo_sync_needed = False

    def _new_device(self):
        dev = tinytuya.OutletDevice(
            self.cfg["device_id"], self.cfg["device_ip"], self.cfg["local_key"]
        )
        dev.set_version(float(self.cfg.get("protocol_version", 3.3)))
        dev.set_socketPersistent(True)
        dev.set_socketTimeout(1.5)
        dev.set_socketRetryLimit(1)
        return dev

    def _commands(self, dev):
        while True:
            try:
                obj, dp, value = self.queue.get_nowait()
            except queue.Empty:
                return
            try:
                if dp is None:
                    dev.status(nowait=True)
                else:
                    # Ne pas attendre la réponse synchrone pour ne pas confondre
                    # notifications spontanées et accusés de réception.
                    result = dev.set_value(dp, value, nowait=True)
                    if isinstance(result, dict) and ("Error" in result or "Err" in result):
                        raise ConnectionError(str(result))
                self.publish(self.root + "/ack", {"time": utc_now(), "ok": True,
                    "action": obj["action"], "value": obj.get("value"),
                    "status": "sent_not_confirmed"}, retain=False)
                self.next_command_query = time.monotonic() + 2
                LOG.info("Commande envoyée : %s", obj)
            except Exception as exc:
                self.publish(self.root + "/ack", {"time": utc_now(), "ok": False,
                    "action": obj.get("action"), "error": str(exc)}, retain=False)
                raise

    def run(self):
        mqtt_cfg = self.cfg
        self.client.connect_async(mqtt_cfg.get("mqtt_host", "127.0.0.1"),
                                  int(mqtt_cfg.get("mqtt_port", 1883)), keepalive=60)
        self.client.loop_start()
        delay = 2
        try:
            while not STOP.is_set():
                dev = None
                try:
                    dev = self._new_device()
                    # Ne pas extrapoler la puissance pendant une interruption TCP.
                    self.energy_last_ts = None
                    self.last_data = time.monotonic()
                    # La méthode nowait ouvre la connexion et initie l'état initial.
                    dev.status(nowait=True)
                    poll_every = float(self.cfg.get("poll_seconds", 60))
                    next_poll = time.monotonic() + poll_every
                    next_heartbeat = time.monotonic() + 20
                    LOG.info("Session TCP Feyree ouverte (%s)", self.cfg["device_ip"])
                    while not STOP.is_set():
                        self._commands(dev)
                        now = time.monotonic()
                        if now >= next_heartbeat:
                            dev.heartbeat(nowait=True)
                            next_heartbeat = now + 20
                        if now >= next_poll or (self.next_command_query is not None and now >= self.next_command_query):
                            dev.status(nowait=True)
                            next_poll = now + poll_every
                            self.next_command_query = None
                        data = dev.receive()  # socket timeout: <= 1.5 s
                        if data is not None:
                            self.ingest(data)
                        if time.monotonic() - self.last_data > max(90, 2.5 * poll_every):
                            raise TimeoutError("Aucun état DP reçu depuis trop longtemps")
                    delay = 2
                except Exception as exc:
                    self.set_available(False)
                    # Un ordre de démarrage ne doit jamais être rejoué après
                    # une panne du chargeur ou du Wi-Fi.
                    while True:
                        try:
                            pending, _, _ = self.queue.get_nowait()
                        except queue.Empty:
                            break
                        self.publish(self.root + "/ack", {"time": utc_now(),
                            "ok": False, "action": pending.get("action"),
                            "error": "Commande abandonnée (déconnexion)"}, retain=False)
                    LOG.warning("Connexion Feyree perdue : %s", exc)
                    if not STOP.wait(delay):
                        delay = min(60, delay * 2)
                finally:
                    if dev is not None:
                        try:
                            dev.close()
                        except Exception:
                            pass
        finally:
            self.set_available(False)
            self.client.disconnect()
            self.client.loop_stop()


def main():
    parser = argparse.ArgumentParser(description="Feyree F-M24 <-> MQTT local")
    parser.add_argument("--config", required=True, help="Chemin vers config.json")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    for name in ("device_id", "device_ip", "local_key"):
        if not cfg.get(name):
            parser.error(f"Configuration manquante: {name}")
    if float(cfg.get("poll_seconds", 60)) < 10:
        parser.error("poll_seconds doit être >= 10")
    if float(cfg.get("dp120_publish_seconds", 30)) < 0:
        parser.error("dp120_publish_seconds doit être >= 0")
    signal.signal(signal.SIGINT, lambda *_: STOP.set())
    signal.signal(signal.SIGTERM, lambda *_: STOP.set())
    dz = cfg.get("domoticz") or {}
    if dz.get("enabled"):
        indices = dz.get("idx") or {}
        for field, idx in indices.items():
            if idx is not None and (isinstance(idx, bool) or not
                                    str(idx).isdigit() or int(idx) <= 0):
                parser.error(f"domoticz.idx.{field} doit etre un IDX positif ou null")
        if float(dz.get("power_publish_seconds", 60)) < 5:
            parser.error("domoticz.power_publish_seconds doit etre >= 5")
        if dz.get("write_enabled"):
            if dz.get("topic_in", "domoticz/in") == dz.get("topic_out", "domoticz/out"):
                parser.error("Les topics Domoticz in et out doivent etre distincts")
            if not (indices.get("setpoint") or indices.get("switch")):
                parser.error("domoticz.write_enabled requiert un IDX setpoint ou switch")
    Gateway(cfg).run()


if __name__ == "__main__":
    main()