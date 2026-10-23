# Passerelle Feyree F-M24 ↔ MQTT

Daemon Python **100 % local**, TinyTuya (TCP 6668) + Mosquitto. Aucun appel au cloud Tuya. Compatible avec une instance Domoticz connectée à MQTT. L'interface actuelle de Domoticz peut continuer à fonctionner pendant les essais ; évitez toutefois plusieurs connexions TCP TinyTuya simultanées au chargeur.

## Installation rapide

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp config.example.json config.json
chmod 600 config.json
# Renseignez device_id, device_ip, local_key, protocol_version et MQTT
.venv/bin/python feyree_mqtt.py --config config.json --verbose
```

**Attention** : la version du protocole LAN doit être celle qui fonctionne déjà avec votre script TinyTuya ; `3.3` n'est qu'un exemple. Pour l'adresse IP, créez idéalement une réservation DHCP. Ne publiez pas `config.json` : il contient la clé locale.

## Topics MQTT

Avec `topic: "feyree"` :

| Topic | Contenu | Retained ? |
|---|---|---|
| `feyree/availability` | `online` / `offline` suivant réception effective des états | oui |
| `feyree/state` | JSON complet des DP **mis en cache** + champs décodés + horodatage de la dernière modification | oui |
| `feyree/dp/127` (etc.) | Valeur JSON du DP correspondant, à chaque changement (DP120 limité en fréquence) | oui |
| `feyree/set` | Commande JSON envoyée depuis Domoticz | **non** |
| `feyree/ack` | JSON `sent_not_confirmed` ou erreur | non |

`feyree/state` conserve les DP déjà connus lorsqu'une notification partielle ne contient que le DP 120. Le Feyree émet le compteur de durée **DP120 toutes les secondes pendant la charge** : pour éviter les publications MQTT inutiles, la passerelle garde toutes les valeurs dans son cache mais n'en publie qu'une toutes les **30 secondes par défaut** (avec le snapshot `feyree/state`). Une variation d’un autre DP reste publiée immédiatement, ainsi qu’une remise à zéro du compteur. Modifier `dp120_publish_seconds` dans `config.json` pour ajuster ce délai (`0` pour tout publier). Les données peuvent être anciennes après déconnexion : **vérifiez `feyree/availability`**. Les valeurs publiées dans `decoded` proviennent du mapping F-M24 que nous avons relevé. La remontée immédiate des transitions CP dépend de ce que le firmware pousse effectivement : **à mesurer**.

## Commandes

```sh
mosquitto_pub -t feyree/set -m '{"action":"status"}'
mosquitto_pub -t feyree/set -m '{"action":"stop"}'
mosquitto_pub -t feyree/set -m '{"action":"start"}'
mosquitto_pub -t feyree/set -m '{"action":"current","value":8}'
mosquitto_pub -t feyree/set -m '{"action":"delay","value":15}'
mosquitto_pub -t feyree/set -m '{"action":"wait"}'
```

`start` = DP124 `OpenCharging`, `stop` = DP124 `CloseCharging`, `current` = DP114 (8–16 A), `delay` = DP118 (0–15 heures). **Aucun essai en dessous de 8 A n'est automatisé.** `wait` = DP124 `WaitOperation` : **pause de la charge sans remise à zéro des compteurs d’énergie et de durée**, constatée expérimentalement. À la différence de `stop`, cette commande ne clôture pas la session ; elle ne verrouille toutefois pas la prochaine connexion du véhicule. Les commandes MQTT retained sont refusées pour éviter des départs intempestifs après reconnexion. Les accusés de réception `sent_not_confirmed` signifient uniquement « commande transmise à TinyTuya » ; vérifiez le DP retourné et l'effet réel.

```sh
mosquitto_sub -v -t 'feyree/#'
```

## Stratégie de fonctionnement

La passerelle garde une socket TCP persistante ; elle interroge l'état initial puis écoute `receive()`. Elle envoie un heartbeat toutes les 20 s, effectue un polling de secours toutes les 60 s par défaut et se reconnecte avec backoff. Un seul thread accède à la socket TinyTuya, ce qui évite les accès concurrents du callback MQTT. Pas de logique autonome `delay=15` ni d’autorisation de charge : cette stratégie sera ajoutée ultérieurement dans Domoticz. **Le renouvellement d’un délai différé pendant l’attente a été validé expérimentalement** ; il reste à coder sa politique d’utilisation. En cas de redémarrage du Feyree, son différé s'efface selon vos essais : **ce script ne constitue pas un verrouillage de sécurité**.

## Signal Control Pilot : DP 127 (`cp`)

Sur le Feyree F-M24, le **DP 127** semble correspondre à la tension positive du signal **Control Pilot (CP)**, qui renseigne sur l'état électrique de connexion entre le chargeur et le véhicule (IEC 61851). Cette interprétation est étayée par les mesures suivantes **réalisées sur ce chargeur** :

| Situation observée | DP 127 relevé | État CP / IEC 61851 | Tension CP nominale |
|---|---:|---|---:|
| Voiture débranchée | **11** | **A** — véhicule absent | +12 V |
| Voiture branchée, en attente (*stand-by*) | **9** | **B** — véhicule connecté, non prêt à charger | +9 V |
| Voiture branchée et en charge | **5** | **C** — véhicule prêt pour la charge | +6 V |

Les valeurs 11, 9 et 5 sont **expérimentales**, propres aux mesures affichées par ce F-M24 ; les valeurs IEC 61851 indiquées dans la dernière colonne sont **nominales**. Un écart est possible selon les tolérances et la mesure interne de l'appareil. Les autres états CP (dont D et les états de défaut) n'ont pas été vérifiés sur ce chargeur.

**Exploitation MQTT :**

- `feyree/dp/127` : valeur brute du DP127, publiée lors d'un changement ;
- `feyree/state` : valeur également disponible sous `dps["127"]`, et convertie en `decoded.cp_v` ;
- `feyree/availability` : à consulter pour ne pas interpréter comme actuel un état MQTT retenu après une perte de communication.

Pour la logique Domoticz, le DP127 est un indicateur utile de la **présence physique et de l'état de préparation du véhicule**, complémentaire du DP101 (`DeviceState`). Ne supposez pas que ces trois nombres sont les **seules** valeurs possibles : classez les états par plages validées expérimentalement plutôt que par égalité stricte.

```sh
mosquitto_sub -v -t 'feyree/dp/127'
```

## Service systemd (optionnel)

Le fichier `feyree-mqtt.service` fourni suppose un utilisateur système `feyree`, une installation sous `/opt/feyree-mqtt` et une configuration protégée sous `/etc/feyree-mqtt/config.json`. Adaptez ces chemins et créez l'utilisateur avant d'installer le service.

## Diagnostic

1. Vérifiez `feyree/availability` et la publication initiale dans `feyree/state`.
2. Débranchez / branchez la voiture et notez si un nouveau `feyree/dp/127` arrive spontanément (avant l'interrogation de secours de 60 s).
3. Testez `stop` puis `start` et contrôlez les changements de DP et la puissance réelle.
4. Si la connexion TCP est instable, fermez l'application Smart Life, et vérifiez que votre ancien script ne maintient pas déjà une autre connexion au chargeur.


### Sécurité des commandes pendant les coupures

Si le chargeur est hors ligne, la passerelle rejette les commandes MQTT ; elle ne les conserve pas pour les rejouer lors de la reconnexion. Elle purge aussi toute commande encore en file lors de la perte de session. Ne publiez **jamais** les commandes avec `-r` / `retain=true`.

### Validation

Tests unitaires sans matériel : `python3 -m pytest -q test_feyree_mqtt.py` (nécessite `pytest`). Le script a été vérifié syntaxiquement et par tests simulés ; il n'a **pas** été exécuté contre votre Feyree ni votre broker réel.