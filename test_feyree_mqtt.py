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
