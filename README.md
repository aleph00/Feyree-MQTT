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
| `feyree/dp/127` (etc.) | Valeur JSON du DP correspondant, uniquement à chaque changement | oui |
| `feyree/set` | Commande JSON envoyée depuis Domoticz | **non** |
| `feyree/ack` | JSON `sent_not_confirmed` ou erreur | non |

`feyree/state` conserve les DP déjà connus lorsqu'une notification partielle ne contient que le DP 120. Les données peuvent être anciennes après déconnexion : **vérifiez `feyree/availability`**. Les valeurs publiées dans `decoded` proviennent du mapping F-M24 que nous avons relevé. La remontée immédiate des transitions CP dépend de ce que le firmware pousse effectivement : **à mesurer**.

## Commandes

```sh
mosquitto_pub -t feyree/set -m '{"action":"status"}'
mosquitto_pub -t feyree/set -m '{"action":"stop"}'
mosquitto_pub -t feyree/set -m '{"action":"start"}'
mosquitto_pub -t feyree/set -m '{"action":"current","value":8}'
mosquitto_pub -t feyree/set -m '{"action":"delay","value":15}'
mosquitto_pub -t feyree/set -m '{"action":"wait"}'
```

`start` = DP124 `OpenCharging`, `stop` = DP124 `CloseCharging`, `current` = DP114 (8–16 A), `delay` = DP118 (0–15 heures). **Aucun essai en dessous de 8 A n'est automatisé.** `wait` = `WaitOperation` à titre de diagnostic : on sait que cela ne verrouille pas le prochain branchement. Les commandes MQTT retained sont refusées pour éviter des départs intempestifs après reconnexion. Les accusés de réception `sent_not_confirmed` signifient uniquement « commande transmise à TinyTuya » ; vérifiez le DP retourné et l'effet réel.

```sh
mosquitto_sub -v -t 'feyree/#'
```

## Stratégie de fonctionnement

La passerelle garde une socket TCP persistante ; elle interroge l'état initial puis écoute `receive()`. Elle envoie un heartbeat toutes les 20 s, effectue un polling de secours toutes les 60 s par défaut et se reconnecte avec backoff. Un seul thread accède à la socket TinyTuya, ce qui évite les accès concurrents du callback MQTT. Pas de logique autonome `delay=15` ni d'autorisation de charge : à ajouter seulement lorsque le test de renouvellement du différé sera validé. En cas de redémarrage du Feyree, son différé s'efface selon vos essais : **ce script ne constitue pas un verrouillage de sécurité**.

La valeur CP que vous avez constatée sur le DP127 : **11 voiture débranchée, 9 branchée en attente, 5 en charge**. Elle est publiée en brut et décodée sous `cp_v`.

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