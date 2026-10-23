"""Unit tests without physical charger or MQTT broker."""
import importlib.util
import json
import sys
import types

class MockClient:
    def __init__(self, *args, **kwargs):
        self.messages = []
    def will_set(self, *args, **kwargs): pass
    def reconnect_delay_set(self, *args, **kwargs): pass
    def publish(self, topic, payload, **kwargs):
        self.messages.append((topic, payload))
    def subscribe(self, *args, **kwargs): pass

class MockDevice:
    def __init__(self, *args, **kwargs):
        self.calls=[]
    def set_value(self, dp, value, nowait=False):
        self.calls.append((dp,value,nowait))
        return None

sys.modules.setdefault('tinytuya', types.SimpleNamespace(OutletDevice=MockDevice))
sys.modules.setdefault('paho', types.ModuleType('paho'))
sys.modules.setdefault('paho.mqtt', types.ModuleType('paho.mqtt'))
sys.modules['paho.mqtt.client'] = types.SimpleNamespace(Client=MockClient)
spec = importlib.util.spec_from_file_location('feyree_mqtt', __file__.replace('test_feyree_mqtt.py','feyree_mqtt.py'))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def test_partial_merge():
    g=m.Gateway({'device_id':'test','device_ip':'127.0.0.1','local_key':'dummy'})
    g.ingest({'dps': {'127':11, '102':2295, '114':8}})
    g.ingest({'protocol':4,'data':{'dps':{'120':'00:00:21'}}})
    assert g.dps['127']==11
    assert g.dps['120']=='00:00:21'
    assert g.last_status['decoded']['voltage_v']==229.5
    assert g.last_status['decoded']['cp_v']==11


def test_commands_validation():
    assert m.translate_command({'action':'stop'})==(124,'CloseCharging')
    assert m.translate_command({'action':'current','value':8})==(114,8)
    for x in [7, 17, 9.5, True, '8']:
        try:
            m.translate_command({'action':'current','value':x})
        except ValueError:
            pass
        else:
            raise AssertionError(f'Unexpectedly accepted {x!r}')


def test_retained_and_offline_commands_rejected():
    g=m.Gateway({'device_id':'test','device_ip':'127.0.0.1','local_key':'dummy'})
    msg=types.SimpleNamespace(payload=b'{"action":"start"}', retain=False)
    g.on_message(None,None,msg)
    assert g.queue.empty()
    assert json.loads(g.client.messages[-1][1])['ok'] is False
    g.set_available(True)
    msg.retain=True
    g.on_message(None,None,msg)
    assert g.queue.empty()
    msg.retain=False
    g.on_message(None,None,msg)
    assert g.queue.qsize()==1



def test_dp120_throttle_without_delaying_other_dps(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(m.time, "monotonic", lambda: clock[0])
    g = m.Gateway({'device_id': 'test', 'device_ip': '127.0.0.1',
                   'local_key': 'dummy', 'dp120_publish_seconds': 30})

    g.ingest({'dps': {'120': '00:00:01', '127': 11}})
    assert sum(topic.endswith('/dp/120') for topic, _ in g.client.messages) == 1

    # Le Feyree continue d'envoyer DP120 chaque seconde ; tout est mis en cache.
    for sec in range(2, 31):
        clock[0] = 100 + sec - 1
        g.ingest({'dps': {'120': f'00:00:{sec:02d}'}})
    assert g.dps['120'] == '00:00:30'
    assert sum(topic.endswith('/dp/120') for topic, _ in g.client.messages) == 1

    # À 30 secondes, publication du compteur et de l'état agrégé.
    clock[0] = 130
    g.ingest({'dps': {'120': '00:00:31'}})
    assert sum(topic.endswith('/dp/120') for topic, _ in g.client.messages) == 2

    # Le changement CP est publié immédiatement malgré le délai sur DP120.
    clock[0] = 131
    g.ingest({'dps': {'127': 9, '120': '00:00:32'}})
    assert g.client.messages[-1] == ('feyree/dp/120', '00:00:32')
    assert ('feyree/dp/127', '9') in g.client.messages
    assert g.last_status['decoded']['cp_v'] == 9

    # Une remise à zéro du compteur est publiée sans attendre 30 secondes.
    clock[0] = 132
    g.ingest({'dps': {'120': '00:00:00'}})
    assert g.client.messages[-1] == ('feyree/dp/120', '00:00:00')


def test_dp120_alone_does_not_publish_global_state_every_second(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(m.time, 'monotonic', lambda: clock[0])
    g = m.Gateway({'device_id': 'test', 'device_ip': '127.0.0.1',
                   'local_key': 'dummy', 'dp120_publish_seconds': 30})
    g.ingest({'dps': {'120': '00:00:01'}})
    first = len(g.client.messages)
    clock[0] = 101
    g.ingest({'dps': {'120': '00:00:02'}})
    assert len(g.client.messages) == first
    assert g.dps['120'] == '00:00:02'



def test_domoticz_mqtt_mapping_and_energy_continuity(monkeypatch):
    from io import StringIO
    clock = [100.0]
    monkeypatch.setattr(m.time, "monotonic", lambda: clock[0])

    def fake_api(url, timeout):
        assert 'rid=204' in url
        return StringIO(json.dumps({"result": [{"Data": "4.250 kWh"}]}))

    monkeypatch.setattr(m, "urlopen", fake_api)
    cfg = {'device_id': 'test', 'device_ip': '127.0.0.1',
           'local_key': 'dummy',
           'domoticz': {
               'enabled': True, 'http_url': 'http://127.0.0.1:8080',
               'power_publish_seconds': 30,
               'idx': {'temperature': 200, 'voltage': 201, 'current': 202,
                       'power': 204, 'state': 205, 'cp': 208,
                       'setpoint': 206, 'delay': 209, 'switch': 207}
           }}
    g = m.Gateway(cfg)
    g.mqtt_connected = True
    g.ingest({'dps': {'110': 291, '102': 2300, '105': 82,
                      '109': 18, '101': 'charing', '114': 8, '118': 15,
                      '124': 'OpenCharging', '127': 5}})
    messages = [json.loads(payload) for topic, payload in g.client.messages
                if topic == 'domoticz/in']
    by_idx = {int(payload['idx']): payload for payload in messages}
    assert by_idx[200]['svalue'] == '29.1'
    assert by_idx[201]['svalue'] == '230.0'
    assert by_idx[202]['svalue'] == '8.2'
    assert by_idx[204]['svalue'] == '1800;4250'
    assert by_idx[205]['svalue'] == 'charing'
    assert by_idx[205]['parse'] is True
    assert by_idx[208]['svalue'] == '5'
    assert by_idx[208]['parse'] is True
    assert by_idx[206]['svalue'] == '8'
    assert by_idx[206]['parse'] is False
    assert by_idx[209]['svalue'] == '15'
    assert by_idx[209]['parse'] is False
    assert by_idx[207]['nvalue'] == 1

    # 30 s a 1800 W = 15 Wh, sans creer 30 publications par seconde.
    n = len(messages)
    clock[0] = 130.0
    g.ingest({'dps': {'120': '00:00:30'}})
    messages = [json.loads(payload) for topic, payload in g.client.messages
                if topic == 'domoticz/in']
    assert len(messages) == n + 1
    assert messages[-1]['idx'] == 204
    assert messages[-1]['svalue'] == '1800;4265'

    # Pause ne fait PAS passer l'interrupteur existant à Off.
    clock[0] = 131.0
    g.ingest({'dps': {'124': 'WaitOperation'}})
    messages = [json.loads(payload) for topic, payload in g.client.messages
                if topic == 'domoticz/in']
    assert not any(v['idx'] == 207 and v['nvalue'] == 0 for v in messages)

    clock[0] = 132.0
    g.ingest({'dps': {'124': 'CloseCharging', '109': 0, '101': 'finish'}})
    messages = [json.loads(payload) for topic, payload in g.client.messages
                if topic == 'domoticz/in']
    assert any(v['idx'] == 207 and v['nvalue'] == 0 for v in messages)
    assert any(v['idx'] == 205 and v['svalue'] == 'finish' for v in messages)


def test_domoticz_energy_unavailable_does_not_reset_counter(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(m.time, "monotonic", lambda: clock[0])
    def unavailable(url, timeout):
        raise OSError("Domoticz offline")
    monkeypatch.setattr(m, "urlopen", unavailable)
    cfg = {'device_id':'test', 'device_ip':'127.0.0.1','local_key':'dummy',
           'domoticz': {'enabled':True, 'http_url':'http://127.0.0.1:8080',
                         'idx':{'power': 204, 'voltage': 201}}}
    g = m.Gateway(cfg)
    g.mqtt_connected = True
    g.ingest({'dps': {'102': 2300, '109': 18}})
    payloads = [json.loads(p) for topic, p in g.client.messages
                if topic == 'domoticz/in']
    assert any(v['idx'] == 201 for v in payloads)
    assert not any(v['idx'] == 204 for v in payloads)
    assert g.energy_wh is None


def test_cp_change_triggers_immediate_domoticz_update(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(m.time, "monotonic", lambda: clock[0])
    g = m.Gateway({'device_id': 'test', 'device_ip': '127.0.0.1',
                   'local_key': 'dummy',
                   'domoticz': {'enabled': True, 'idx': {'cp': 208}}})
    g.mqtt_connected = True
    g.ingest({'dps': {'127': 11}})
    clock[0] += 0.5
    g.ingest({'dps': {'127': 9}})
    clock[0] += 0.5
    g.ingest({'dps': {'127': 5}})
    messages = [json.loads(payload) for topic, payload in g.client.messages
                if topic == 'domoticz/in']
    assert [msg['svalue'] for msg in messages] == ['11', '9', '5']
    assert all(msg['idx'] == 208 and msg['parse'] is True for msg in messages)


def test_read_domoticz_energy_wh_accepts_real_counter(monkeypatch):
    from io import StringIO

    def fake_api(url, timeout):
        assert "rid=204" in url
        return StringIO(json.dumps({
            "result": [{"Data": "2825.173 kWh"}]
        }))

    monkeypatch.setattr(m, "urlopen", fake_api)
    assert m.read_domoticz_energy_wh("http://localhost:8080", 204) == 2825173.0


def test_domoticz_out_to_feyree_commands():
    idx = {"setpoint": 206, "switch": 207}
    translate = m.translate_domoticz_command

    assert translate({"idx": 206, "svalue1": "13.0"}, idx) == {
        "action": "current", "value": 13
    }
    assert translate({"idx": "207", "nvalue": 1, "svalue1": "On"}, idx) == {
        "action": "start"
    }
    assert translate({"idx": 207, "nvalue": 0}, idx) == {"action": "stop"}
    assert translate({"idx": 207, "svalue1": "Off"}, idx) == {"action": "stop"}
    assert translate({"idx": 208, "nvalue": 1}, idx) is None
    assert translate({"nvalue": 1}, idx) is None

    for value in ["7", "17", "8.5", "NaN", "", None]:
        try:
            translate({"idx": 206, "svalue1": value}, idx)
        except ValueError:
            pass
        else:
            raise AssertionError(f"Commande invalide acceptée : {value!r}")


def test_domoticz_out_route_opt_in_retained_and_offline():
    cfg = {
        'device_id': 'test', 'device_ip': '127.0.0.1',
        'local_key': 'dummy', 'domoticz': {
            'enabled': True,
            'write_enabled': True,
            'topic_out': 'domoticz/out',
            'idx': {'setpoint': 206, 'switch': 207}
        }
    }
    g = m.Gateway(cfg)
    # Le mode écriture n'envoie jamais de commande si Feyree est hors ligne.
    msg = types.SimpleNamespace(
        topic='domoticz/out',
        payload=b'{"idx":207,"nvalue":1}', retain=False)
    g.on_message(None, None, msg)
    assert g.queue.empty()
    assert json.loads(g.client.messages[-1][1])['ok'] is False

    g.set_available(True)
    msg.retain = True
    g.on_message(None, None, msg)
    assert g.queue.empty()

    msg.retain = False
    g.on_message(None, None, msg)
    assert g.queue.get_nowait() == ({'action': 'start'}, 124, 'OpenCharging')

    msg.payload = b'{"idx":206,"svalue1":"10.0"}'
    g.on_message(None, None, msg)
    assert g.queue.get_nowait() == (
        {'action': 'current', 'value': 10}, 114, 10)

    # Les autres dispositifs ne déclenchent rien.
    msg.payload = b'{"idx":999,"nvalue":1}'
    g.on_message(None, None, msg)
    assert g.queue.empty()

    # Un topic inexistant/quelconque n'est jamais interprete.
    msg.topic = 'domoticz/in'
    msg.payload = b'{"idx":207,"nvalue":0}'
    g.on_message(None, None, msg)
    assert g.queue.empty()

    # L'intégration reste facultative (désactivée = aucune commande Domoticz).
    cfg['domoticz']['write_enabled'] = False
    g2 = m.Gateway(cfg)
    g2.set_available(True)
    msg.topic = 'domoticz/out'
    g2.on_message(None, None, msg)
    assert g2.queue.empty()


def test_domoticz_on_connect_subscribes_only_when_write_enabled():
    g = m.Gateway({'device_id': 'test', 'device_ip': '127.0.0.1',
                   'local_key': 'dummy',
                   'domoticz': {'enabled': True, 'write_enabled': True,
                                'idx': {'setpoint': 206}}})
    subscribed = []
    g.client.subscribe = lambda topic, qos=0: subscribed.append((topic, qos))
    g.on_connect(g.client, None, None, 0)
    assert ('feyree/set', 1) in subscribed
    assert ('domoticz/out', 1) in subscribed

    g2 = m.Gateway({'device_id': 'test', 'device_ip': '127.0.0.1',
                    'local_key': 'dummy',
                    'domoticz': {'enabled': True, 'write_enabled': False,
                                 'idx': {'setpoint': 206}}})
    subscribed2 = []
    g2.client.subscribe = lambda topic, qos=0: subscribed2.append((topic, qos))
    g2.on_connect(g2.client, None, None, 0)
    assert ('domoticz/out', 1) not in subscribed2


def test_domoticz_delay_setpoint_bidirectional_without_loops(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(m.time, "monotonic", lambda: clock[0])
    cfg = {'device_id': 'test', 'device_ip': '127.0.0.1',
           'local_key': 'dummy',
           'domoticz': {'enabled': True, 'write_enabled': True,
                         'idx': {'delay': 209}}}
    g = m.Gateway(cfg)
    g.mqtt_connected = True
    g.ingest({'dps': {'118': 15}})
    published = [json.loads(payload) for topic, payload in g.client.messages
                 if topic == 'domoticz/in']
    assert published[-1] == {
        'command': 'udevice', 'idx': 209, 'nvalue': 0, 'svalue': '15',
        'parse': False
    }

    # Nouvelle valeur locale : publication immédiate, sans déclenchement de script.
    clock[0] += 1
    g.ingest({'dps': {'118': 12}})
    published = [json.loads(payload) for topic, payload in g.client.messages
                 if topic == 'domoticz/in']
    assert published[-1]['svalue'] == '12'
    assert published[-1]['parse'] is False
    assert len(published) == 2

    # Les updates identiques n'engendrent pas de publication supplémentaire.
    g.ingest({'dps': {'118': 12}})
    published = [json.loads(payload) for topic, payload in g.client.messages
                 if topic == 'domoticz/in']
    assert len(published) == 2

    # La consigne issue de Domoticz repart vers DP118, dans la plage autorisée.
    msg = types.SimpleNamespace(topic='domoticz/out', retain=False,
                                payload=b'{"idx":209,"svalue1":"15.0"}')
    g.on_message(None, None, msg)
    assert g.queue.get_nowait() == (
        {'action': 'delay', 'value': 15}, 118, 15)
    # Renouveler la même durée reste possible par une nouvelle commande.
    g.on_message(None, None, msg)
    assert g.queue.get_nowait() == (
        {'action': 'delay', 'value': 15}, 118, 15)


def test_domoticz_delay_setpoint_rejects_invalid_values():
    idx = {'delay': 209}
    for raw in ('-1', '16', '7.5', 'NaN', 'Infinity', '', None):
        try:
            m.translate_domoticz_command({'idx': 209, 'svalue1': raw}, idx)
        except ValueError:
            pass
        else:
            raise AssertionError(f'Invalid delay accepted: {raw!r}')
    assert m.translate_domoticz_command({'idx': 209, 'svalue1': '0'}, idx) == {
        'action': 'delay', 'value': 0
    }
    assert m.translate_domoticz_command({'idx': 209, 'svalue1': '15'}, idx) == {
        'action': 'delay', 'value': 15
    }


def test_domoticz_periodic_refresh_only_measurements(monkeypatch):
    # Aucun DP ne change : les courbes doivent recevoir un point <= 300 s,
    # sans réémettre le moindre setpoint ou sélecteur, ni réveiller les scripts.
    clock = [100.0]
    monkeypatch.setattr(m.time, "monotonic", lambda: clock[0])
    cfg = {'device_id': 'test', 'device_ip': '127.0.0.1',
           'local_key': 'dummy',
           'domoticz': {
               'enabled': True, 'measurement_refresh_seconds': 300,
               'idx': {'voltage': 201, 'current': 202,
                       'temperature': 203, 'state': 205, 'cp': 208,
                       'setpoint': 206, 'delay': 209, 'switch': 207}
           }}
    g = m.Gateway(cfg)
    g.mqtt_connected = True
    g.ingest({'dps': {'102': 2300, '105': 0, '110': 220,
                      '101': 'finish', '127': 11, '114': 8,
                      '118': 15, '124': 'CloseCharging'}})

    def messages():
        return [json.loads(p) for t, p in g.client.messages if t == 'domoticz/in']

    assert len(messages()) == 8
    clock[0] = 399.0
    g.update_domoticz({}, clock[0])   # matériel vivant, aucune variation
    assert len(messages()) == 8

    clock[0] = 400.0
    g.update_domoticz({}, clock[0])
    later = messages()[8:]
    assert {p['idx'] for p in later} == {201, 202, 203, 205, 208}
    assert len(later) == 5
    # Un simple rafraîchissement ne doit pas réveiller les timers Lua.
    assert all(p['parse'] is False for p in later)

    clock[0] = 401
    g.ingest({'dps': {'127': 9}})
    assert messages()[-1]['idx'] == 208
    assert messages()[-1]['svalue'] == '9'
    assert messages()[-1]['parse'] is True  # vraie transition CP


def test_domoticz_stable_power_is_republished(monkeypatch):
    from io import StringIO
    clock = [100.0]
    monkeypatch.setattr(m.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(m, "urlopen",
                        lambda url, timeout: StringIO(json.dumps(
                            {"result": [{"Data": "100.000 kWh"}]})))
    g = m.Gateway({
        'device_id': 'test', 'device_ip': '127.0.0.1', 'local_key': 'dummy',
        'domoticz': {'enabled': True, 'http_url': 'http://localhost:8080',
                     'power_publish_seconds': 60, 'idx': {'power': 204}}
    })
    g.mqtt_connected = True
    g.ingest({'dps': {'109': 0}})

    def powers():
        return [json.loads(p) for t, p in g.client.messages
                if t == 'domoticz/in' and json.loads(p)['idx'] == 204]

    assert len(powers()) == 1
    assert powers()[0]['svalue'] == '0;100000'
    clock[0] = 159.9
    g.update_domoticz({}, clock[0])
    assert len(powers()) == 1
    clock[0] = 160
    g.update_domoticz({}, clock[0])
    assert len(powers()) == 2
    assert powers()[-1]['svalue'] == '0;100000'
    clock[0] = 400
    g.update_domoticz({}, clock[0])
    assert len(powers()) == 3


def test_selector_commands_use_level_not_nvalue():
    idx = {'switch': 207}
    tr = lambda p: m.translate_domoticz_command(p, idx, 'selector')
    assert tr({'idx': 207, 'nvalue': 0, 'svalue1': '0'}) == {'action': 'stop'}
    assert tr({'idx': 207, 'nvalue': 2, 'svalue1': '10'}) == {'action': 'wait'}
    assert tr({'idx': 207, 'nvalue': 1, 'svalue1': '20'}) == {'action': 'start'}
    # Certaines versions de Domoticz utilisent nvalue=2 pour les deux niveaux.
    assert tr({'idx': 207, 'nvalue': 2, 'svalue1': '20'}) == {'action': 'start'}
    for value in ('5', '30', '10.5', '', None):
        try:
            tr({'idx': 207, 'svalue1': value})
        except ValueError:
            pass
        else:
            raise AssertionError(f'Niveau inattendu accepté : {value!r}')

    # Le mode interrupteur historique reste inchangé par défaut.
    assert m.translate_domoticz_command(
        {'idx': 207, 'nvalue': 1}, idx) == {'action': 'start'}


def test_selector_device_reflects_wait_start_stop(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(m.time, "monotonic", lambda: clock[0])
    g = m.Gateway({
        'device_id': 'test', 'device_ip': '127.0.0.1', 'local_key': 'dummy',
        'domoticz': {'enabled': True, 'write_enabled': True,
                     'switch_mode': 'selector', 'idx': {'switch': 207}}
    })
    g.mqtt_connected = True
    for operation in ('WaitOperation', 'OpenCharging', 'CloseCharging'):
        g.ingest({'dps': {'124': operation}})
        clock[0] += 1
    updates = [json.loads(p) for t, p in g.client.messages
               if t == 'domoticz/in']
    assert [(p['nvalue'], p['svalue']) for p in updates] == [
        (1, '10'), (1, '20'), (0, '0')]
    assert all(p['parse'] is False for p in updates)

    # Les consignes retour de Domoticz passent par la même file sécurisée.
    for level, action, dp_value in [
        ('10', 'wait', 'WaitOperation'),
        ('20', 'start', 'OpenCharging'),
        ('0', 'stop', 'CloseCharging')
    ]:
        msg = types.SimpleNamespace(topic='domoticz/out', retain=False,
                payload=json.dumps({'idx': 207, 'nvalue': 2,
                                    'svalue1': level}).encode())
        g.on_message(None, None, msg)
        obj, dp, value = g.queue.get_nowait()
        assert (obj['action'], dp, value) == (action, 124, dp_value)
