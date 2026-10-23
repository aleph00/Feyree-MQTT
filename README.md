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

## Intégration des dispositifs Domoticz existants

Cette fonctionnalité est **facultative et désactivée par défaut**. Elle utilise le
client MQTT déjà présent dans Domoticz et **ne crée aucun dispositif**.

Dans votre `config.json`, ajoutez un bloc `domoticz` à la racine et remplacez
les valeurs `null` par les **IDX de vos dispositifs existants** :

```json
"domoticz": {
  "enabled": true,
  "topic_in": "domoticz/in",
  "topic_out": "domoticz/out",
  "write_enabled": false,
  "switch_mode": "switch",
  "measurement_refresh_seconds": 300,
  "http_url": "http://127.0.0.1:8080",
  "power_publish_seconds": 60,
  "idx": {
    "temperature": null,
    "voltage": null,
    "current": null,
    "power": null,
    "state": null,
    "fault": null,
    "cp": null,
    "setpoint": null,
    "delay": null,
    "switch": null
  }
}
```

Vous pouvez laisser un IDX à `null` pour ignorer le dispositif correspondant.
Le mapping est le suivant :

| Clé d'IDX | Dispositif existant | Source Feyree | Mise à jour |
|---|---|---|---|
| `temperature` | Température Chargeur VE | DP 110 | °C, division par 10 |
| `voltage` | Tension Charge VE | DP 102 | V, division par 10 |
| `current` | Courant Charge VE | DP 105 | A, division par 10 |
| `power` | Puissance Charge VE | DP 109 | W, multiplication par 100 + cumul Wh |
| `state` | État VE (texte) | DP 101 | Texte brut, **`charing`** compris |
| `fault` | Défauts chargeur VE (texte) | DP 10 | Bitmap 16 bits décodé : `valeur - erreur1, erreur2` |
| `cp` | Control Pilot (capteur personnalisé en V) | DP 127 | Valeur entière mesurée, par exemple 11/9/5 V |
| `setpoint` | Consigne courant VE | DP 114 | A, synchronisation sans déclenchement |
| `delay` | Durée départ différé (setpoint) | DP 118 | Heures entières, de 0 à 15 h, synchronisation sans déclenchement |
| `switch` | Interrupteur ou sélecteur de charge VE | DP 124 | Mode sélecteur : 0=Off, 10=Pause, 20=On |

En mode historique `"switch_mode": "switch"`, `WaitOperation` ne change
pas l'interrupteur On/Off. En mode `"switch_mode": "selector"`, les trois
états DP124 sont reflétés par le sélecteur 0=Off / 10=Pause / 20=On,
avec `parse: false` pour ne pas renvoyer de commandes en boucle.

Les remontées d'état vers Domoticz fonctionnent dès que `domoticz.enabled`
est à `true`. Le trajet inverse est **facultatif**, avec
`domoticz.write_enabled` réglé à `false` par défaut.

Pour piloter le Feyree à partir des **dispositifs Domoticz déjà existants**,
mettez `write_enabled: true` (avec la syntaxe JSON habituelle) après
vérification des réglages du matériel **MQTT Client Gateway** :
**Publish Topic : out (Flat)** et surtout **Prevent Loop : True**.
La passerelle écoute alors `domoticz/out` et n'interprète que les IDX
`setpoint`, `delay` et `switch` que vous avez configurés :

| Action dans Domoticz | Commande TinyTuya |
|---|---|
| Setpoint modifié à 8…16 A | DP114 = intensité demandée |
| Durée départ différé modifiée à 0…15 h | DP118 = nouvelle durée de différé |
| Switch On | DP124 = `OpenCharging` |
| Switch Off (mode historique) | DP124 = `CloseCharging` |
| Sélecteur niveau 0 — Off | DP124 = `CloseCharging` |
| Sélecteur niveau 10 — Pause | DP124 = `WaitOperation` |
| Sélecteur niveau 20 — On | DP124 = `OpenCharging` |

Un message `domoticz/out` ne correspondant pas à ces trois IDX est ignoré.
Les commandes retenues MQTT, non valides, ou reçues pendant que le Feyree
est hors ligne ne sont **jamais rejouées**. Les commandes directes sur
`feyree/set` continuent de fonctionner. La commande **Pause** est disponible sur le sélecteur 0/10/20
si `domoticz.switch_mode` vaut `"selector"`; la commande
`{"action":"wait"}` sur `feyree/set` reste également possible.

**Évitez d'exécuter en parallèle votre ancienne logique Lua d'envoi
de commandes vers Tuya** : elle pourrait doubler ou contredire les
commandes prises en charge par `domoticz/out`.
Les valeurs miroir des deux setpoints (courant et différé) et du switch/sélecteur sont publiées avec
`parse: false` pour éviter de déclencher une boucle de commandes ; **`state`
et `cp` sont publiés avec `parse: true`** afin de permettre à vos
automatismes Domoticz de réagir immédiatement aux changements.

Le CP conserve les valeurs **brutes du DP 127**, sans conversion en état
logique. Vos relevés du F-M24 sont : **11 V** voiture absente, **9 V**
voiture branchée en attente, **5 V** en charge. Vous pourrez ainsi
exploiter directement le passage `11 → 9` pour armer le différé,
sans que la passerelle décide elle-même quand charger.

**Différé :** créer un dispositif virtuel Domoticz de type *Setpoint*
nommé par exemple « Durée départ différé ». Dans les réglages
du Setpoint, renseignez l'unité **h**, un pas de **1**, un minimum de **0**
et un maximum de **15** (options des versions récentes de Domoticz), puis
placez son IDX dans `domoticz.idx.delay`. La passerelle accepte
uniquement une durée **entière entre 0 et 15 heures** : tout nombre
fractionnaire ou hors plage est refusé. Elle répercute aussi les
modifications locales ou faites dans l'application Tuya, dès qu'un nouveau
DP118 est reçu. L'envoi répété de `15` peut renouveler le différé
pendant l'attente, conformément à votre essai ; **aucun renouvellement
automatique n'est ajouté dans la passerelle**. Cette décision restera dans
vos scripts Domoticz.

### Texte de diagnostic DP10 (bitmap `fault`)

Créez un dispositif virtuel Domoticz de type **Text**, nommé par exemple
« Défauts chargeur VE ». Placez son IDX dans `domoticz.idx.fault`.
La passerelle conserve les bits du DP10 tels que le F-M24 les envoie
et produit une chaîne de la forme :

```text
0 - aucune erreur
1 - err_leak
5 - err_leak, err_temp
512 - err_ocp
```

Le décodage utilise l'ordre des 16 libellés fourni par **votre**
`devices.json` Tuya :

| Bit | Valeur | Libellé Tuya |
|---:|---:|---|
| 0 | 1 | `err_leak` |
| 1 | 2 | `err_cp` |
| 2 | 4 | `err_temp` |
| 3 | 8 | `warn_a_uvp` |
| 4 | 16 | `warn_b_uvp` |
| 5 | 32 | `warn_c_uvp` |
| 6 | 64 | `err_a_ovp` |
| 7 | 128 | `err_b_ovp` |
| 8 | 256 | `err_c_ovp` |
| 9 | 512 | `err_ocp` |
| 10 | 1024 | `err_10` |
| 11 | 2048 | `err_11` |
| 12 | 4096 | `err_esb` |
| 13 | 8192 | `warn_pe_sck` |
| 14 | 16384 | `err_leak_sck` |
| 15 | 32768 | `err_pe` |

Les éventuels bits non documentés sont indiqués comme
`bit_16_inconnu`, etc., plutôt qu'ignorés.
La remontée est **immédiate sur changement**, sans rafraîchissement
à cinq minutes ; une resynchronisation a lieu après reconnexion MQTT.
Aucune commande n'est envoyée au chargeur à partir de ce texte.
Le diagnostic ne remplace pas les protections électriques intégrées
au matériel.

### Rafraîchissement périodique des mesures (courbes Domoticz)

En plus des publications **immédiates sur changement**, la passerelle
rafraîchit les mesures toutes les **300 secondes au maximum** (5 minutes)
lorsqu'elle fonctionne et possède un dernier état du Feyree :

- température `temperature` (DP110), tension `voltage` (DP102) ;
- courant mesuré `current` (DP105) ;
- CP `cp` (DP127).

Les **champs texte** `state` (DP101) et `fault` (DP10)
ne sont **pas rafraîchis périodiquement** : ils sont mis à jour
uniquement lorsque leur valeur change ou lors d'une resynchronisation.

La puissance / énergie `power` (DP109 + intégration) reste publiée
au minimum toutes les `power_publish_seconds` secondes (60 s par défaut),
même lorsqu'elle ne varie pas, et immédiatement lorsqu'elle change.

Les **setpoints `setpoint` et `delay`, ainsi que le `switch` /
sélecteur**, ne sont **jamais** republiés pour ce seul motif. Ils sont
synchronisés seulement lors de modifications ou reconnexions MQTT.

Les rafraîchissements identiques du `cp` ont `parse: false` :
**ils ne relancent pas vos événements Domoticz**.
Les véritables transitions des champs `state`, `fault` et `cp`
conservent `parse: true`.

Le paramètre optionnel `domoticz.measurement_refresh_seconds` vaut
`300` par défaut, avec une plage autorisée de 10 à 300 secondes.
Aucune mesure n'est inventée si le chargeur n'a pas encore envoyé son DP ;
la passerelle ne promet pas de publier si le Feyree ou MQTT est hors ligne.

### Sélecteur Off / Pause / On

Pour exploiter `WaitOperation` directement depuis Domoticz :

1. Créer un dispositif virtuel **Selector Switch** avec les niveaux
   **0=Off, 10=Pause, 20=On** (nom des niveaux : `Off|Pause|On`,
   niveau Off visible).
2. Remplacer `domoticz.idx.switch` par l'IDX de ce nouveau sélecteur.
3. Régler `domoticz.switch_mode` sur `"selector"` et conserver
   `domoticz.write_enabled: true` si l'écriture fonctionne déjà.
4. Redémarrer le service et tester les trois niveaux.

La passerelle interprète le **niveau `svalue1`** publié par
`domoticz/out`, et **non pas `nvalue`**, qui peut valoir 1 ou 2
pour plusieurs niveaux actifs selon Domoticz. Elle valide strictement
0, 10 ou 20 pour éviter d'envoyer une commande inattendue.

| Sélecteur | Commande Feyree | Retour Domoticz |
|---|---|---|
| Off (0) | `CloseCharging` | `nvalue=0, svalue="0"` |
| Pause (10) | `WaitOperation` | `nvalue=1, svalue="10"` |
| On (20) | `OpenCharging` | `nvalue=1, svalue="20"` |

La réception de `WaitOperation` à partir de l'application ou du
chargeur met elle aussi le sélecteur sur **Pause**.
Le mode précédent `"switch"` reste activé par défaut, afin que la
mise à jour du code ne transforme pas l'ancien interrupteur en sélecteur
sans modification volontaire de `config.json`.

### Conservation du compteur d'énergie

**Avant l'activation :** vérifiez l'IDX du compteur `Puissance Charge VE` et
l'adresse HTTP de Domoticz. Ce compteur doit être un dispositif **Electricity
(instant + counter)** et conserver le mode **Energy read: From Device** si
c'était son mode précédent.

Au démarrage, la passerelle lit via l'API HTTP le **total existant en kWh**
puis le convertit en Wh. Elle n'écrit jamais une valeur nulle à la place du
compteur si cette lecture échoue (par exemple lorsque l'API exige une
authentification). Elle réessaie toutes les 60 s jusqu'à la réussite.

Le cumul d'énergie est ensuite intégré sur la base du temps effectivement
écoulé et de la puissance mesurée précédemment (pas à chaque message DP 120).
La puissance et le total Wh sont envoyés à `domoticz/in` toutes les
`power_publish_seconds` (60 s par défaut), ou immédiatement si la puissance
change. Les intervalles de communication excessivement longs ne sont pas
extrapolés pour éviter de créer des consommations fictives.

**Ne laissez pas tourner l'ancien polling Lua en parallèle**, en particulier
sa ligne qui ajoute `DeviceW/60` : cela doublerait les incréments d'énergie.
Vous pourrez le désactiver après vérification des mises à jour MQTT.

### Automatisations existantes

La passerelle ne gère **ni le timer `$timer Charge Voiture` ni le départ
différé** : ces règles restent dans Domoticz. Votre ancien code de polling
contenait une détection de transitions (`charing` → `finish` /
`no_connet` et inversement). Si vous supprimez son exécution périodique,
conservez cette logique dans un événement Domoticz déclenché par la mise à
jour du dispositif `state` (ou équivalent dzVents), afin de maintenir le
timer et ses `Off AFTER 300` existants.

**Test recommandé :** activez d'abord uniquement les IDX `state`,
`cp`, `voltage`, `current` et `setpoint`, puis `power` après avoir vérifié
que l'API retourne bien le total d'énergie. Un IDX invalide ne doit jamais
être remplacé par un nouvel IDX créé pour l'occasion.

Pour tester ensuite le **sens Domoticz → Feyree** sans ambiguïté :
1. Conserver `write_enabled: false` et vérifier, avec
   `mosquitto_sub -v -t domoticz/out`, qu'un changement manuel de la
   consigne publie bien l'IDX et un champ `svalue1` numérique.
2. Activer `write_enabled: true`, redémarrer la passerelle et effectuer
   un petit changement de consigne (par exemple de 8 à 9 A).
3. Vérifier `feyree/ack` et la remontée du nouveau DP114, puis
   tester On/Off seulement dans des conditions de charge maîtrisées.

Le sujet `domoticz/out` doit bien correspondre au mode de publication
de votre MQTT Client Gateway ; le mode *Index* utilise d'autres topics
(`domoticz/out/<idx>`) et n'est pas pris en charge par cette configuration
sans adaptation.

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