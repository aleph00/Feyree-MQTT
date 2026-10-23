#!/usr/bin/env python3
"""Passerelle locale Feyree F-M24 <-> MQTT (TinyTuya, sans cloud).

Un seul thread dialogue avec le chargeur; le callback MQTT ne fait qu'empiler
les commandes. Pas de stratégie de charge automatique dans cette version.
"""

import argparse
import json
import logging
import queue
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

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
        return 124, "WaitOperation"  # conservé pour diagnostic, pas un verrou
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


class Gateway:
    def __init__(self, config):
        self.cfg = config
        self.root = config.get("topic", "feyree").rstrip("/")
        self.queue = queue.Queue(maxsize=100)
        self.dps = {}
        self.available = False
        self.last_data = 0.0
        self.last_status = None
        self.mqtt_connected = False
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
        client.subscribe(self.root + "/set", qos=1)
        # Une reconnexion du broker ne transforme pas un état périmé en état frais.
        self.publish_availability()
        if self.last_status is not None:
            self.publish(self.root + "/state", self.last_status, retain=True)
        LOG.info("Connecté au broker MQTT")

    def on_disconnect(self, client, userdata, flags_or_rc, reason_code=None, *args):
        self.mqtt_connected = False
        LOG.warning("MQTT déconnecté")

    def on_message(self, client, userdata, msg):
        if msg.retain:
            LOG.warning("Commande MQTT retenue ignorée (sécurité)")
            return
        try:
            obj = json.loads(msg.payload.decode("utf-8"))
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
        changed = {k: v for k, v in dps.items() if k not in self.dps or self.dps[k] != v}
        self.dps.update(dps)
        self.last_data = time.monotonic()
        self.set_available(True)
        if changed:
            snapshot = {
                "time": utc_now(),
                "dps": dict(self.dps),
                "decoded": decode(self.dps),
            }
            self.last_status = snapshot
            self.publish(self.root + "/state", snapshot)
            for dp, value in changed.items():
                self.publish(self.root + "/dp/" + dp, value)
            LOG.debug("DP modifiés : %s", changed)
        return True

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
    signal.signal(signal.SIGINT, lambda *_: STOP.set())
    signal.signal(signal.SIGTERM, lambda *_: STOP.set())
    Gateway(cfg).run()


if __name__ == "__main__":
    main()