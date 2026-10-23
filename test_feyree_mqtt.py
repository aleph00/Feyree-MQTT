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
