"""
Run common tests on server side
Tests that can only be run on server side must be defined here
"""
import asyncio
import pytest
import logging
import os
import shelve

from .tests_common import CommonTests, add_server_methods
from .tests_xml import XmlTests
from .tests_subscriptions import SubscriptionTests
from datetime import timedelta, datetime
from tempfile import NamedTemporaryFile

import opcua
from opcua import Server
from opcua import Client
from opcua import ua
from opcua import uamethod
from opcua.common.event_objects import BaseEvent, AuditEvent, AuditChannelEvent, AuditSecurityEvent, \
    AuditOpenSecureChannelEvent
from opcua.common import ua_utils

port_num = 48540
port_discovery = 48550
pytestmark = pytest.mark.asyncio
_logger = logging.getLogger(__name__)


@pytest.fixture()
async def server():
    # start our own server
    srv = Server()
    await srv.init()
    srv.set_endpoint(f'opc.tcp://127.0.0.1:{port_num}')
    await add_server_methods(srv)
    await srv.start()
    yield srv
    # stop the server
    await srv.stop()


@pytest.fixture()
async def discovery_server():
    # start our own server
    srv = Server()
    await srv.init()
    await srv.set_application_uri('urn:freeopcua:python:discovery')
    srv.set_endpoint(f'opc.tcp://127.0.0.1:{port_discovery}')
    await srv.start()
    yield srv
    # stop the server
    await srv.stop()


async def test_discovery(server, discovery_server):
    client = Client(discovery_server.endpoint.geturl())
    async with client:
        servers = await client.find_servers()
        new_app_uri = 'urn:freeopcua:python:server:test_discovery'
        server.application_uri = new_app_uri
        await server.register_to_discovery(discovery_server.endpoint.geturl(), 0)
        # let server register registration
        await asyncio.sleep(0.1)
        new_servers = await client.find_servers()
        assert len(new_servers) - len(servers) == 1
        assert new_app_uri not in [s.ApplicationUri for s in servers]
        assert new_app_uri in [s.ApplicationUri for s in new_servers]


async def test_find_servers2(server, discovery_server):
    client = Client(discovery_server.endpoint.geturl())
    async with client:
        servers = await client.find_servers()
        new_app_uri1 = 'urn:freeopcua:python:server:test_discovery1'
        server.application_uri = new_app_uri1
        await server.register_to_discovery(discovery_server.endpoint.geturl(), period=0)
        new_app_uri2 = 'urn:freeopcua:python:test_discovery2'
        server.application_uri = new_app_uri2
        await server.register_to_discovery(discovery_server.endpoint.geturl(), period=0)
        await asyncio.sleep(0.1)  # let server register registration
        new_servers = await client.find_servers()
        assert len(new_servers) - len(servers) == 2
        assert new_app_uri1 not in [s.ApplicationUri for s in servers]
        assert new_app_uri2 not in [s.ApplicationUri for s in servers]
        assert new_app_uri1 in [s.ApplicationUri for s in new_servers]
        assert new_app_uri2 in [s.ApplicationUri for s in new_servers]
        # now do a query with filer
        new_servers = await client.find_servers(['urn:freeopcua:python:server'])
        assert len(new_servers) - len(servers) == 0
        assert new_app_uri1 in [s.ApplicationUri for s in new_servers]
        assert new_app_uri2 not in [s.ApplicationUri for s in new_servers]
        # now do a query with filer
        new_servers = await client.find_servers(['urn:freeopcua:python'])
        assert len(new_servers) - len(servers) == 2
        assert new_app_uri1 in [s.ApplicationUri for s in new_servers]
        assert new_app_uri2 in [s.ApplicationUri for s in new_servers]


async def test_register_namespace(server):
    uri = 'http://mycustom.Namespace.com'
    idx1 = await server.register_namespace(uri)
    idx2 = await server.get_namespace_index(uri)
    assert idx1 == idx2


async def test_register_existing_namespace(server):
    uri = 'http://mycustom.Namespace.com'
    idx1 = await server.register_namespace(uri)
    idx2 = await server.register_namespace(uri)
    idx3 = await server.get_namespace_index(uri)
    assert idx1 == idx2
    assert idx1 == idx3


async def test_register_use_namespace(server):
    uri = 'http://my_very_custom.Namespace.com'
    idx = await server.register_namespace(uri)
    root = server.get_root_node()
    myvar = await root.add_variable(idx, 'var_in_custom_namespace', [5])
    myid = myvar.nodeid
    assert idx == myid.NamespaceIndex


async def test_server_method(server):
    def func(parent, variant):
        variant.Value *= 2
        return [variant]

    o = server.get_objects_node()
    v = await o.add_method(3, 'Method1', func, [ua.VariantType.Int64], [ua.VariantType.Int64])
    result = o.call_method(v, ua.Variant(2.1))
    assert result == 4.2


async def test_historize_variable(server):
    o = server.get_objects_node()
    var = await o.add_variable(3, "test_hist", 1.0)
    await server.iserver.enable_history_data_change(var, timedelta(days=1))
    await asyncio.sleep(1)
    await var.set_value(2.0)
    await var.set_value(3.0)
    await server.iserver.disable_history_data_change(var)


async def test_historize_events(server):
    srv_node = server.get_node(ua.ObjectIds.Server)
    assert await srv_node.get_event_notifier() == {ua.EventNotifier.SubscribeToEvents}
    srvevgen = await server.get_event_generator()
    await server.iserver.enable_history_event(srv_node, period=None)
    assert await srv_node.get_event_notifier() == {ua.EventNotifier.SubscribeToEvents, ua.EventNotifier.HistoryRead}
    srvevgen.trigger(message='Message')
    await server.iserver.disable_history_event(srv_node)


async def test_references_for_added_nodes_method(server):
    objects = server.get_objects_node()
    o = await objects.add_object(3, 'MyObject')
    nodes = await objects.get_referenced_nodes(refs=ua.ObjectIds.Organizes, direction=ua.BrowseDirection.Forward,
                                               includesubtypes=False)
    assert o in nodes
    nodes = await o.get_referenced_nodes(refs=ua.ObjectIds.Organizes, direction=ua.BrowseDirection.Inverse,
                                         includesubtypes=False)
    assert objects in nodes
    assert await o.get_parent() == objects
    assert (await o.get_type_definition()).Identifier == ua.ObjectIds.BaseObjectType

    @uamethod
    def callback(parent):
        return

    m = await o.add_method(3, 'MyMethod', callback)
    nodes = await o.get_referenced_nodes(refs=ua.ObjectIds.HasComponent, direction=ua.BrowseDirection.Forward,
                                         includesubtypes=False)
    assert m in nodes
    nodes = await m.get_referenced_nodes(refs=ua.ObjectIds.HasComponent, direction=ua.BrowseDirection.Inverse,
                                         includesubtypes=False)
    assert o in nodes
    assert await m.get_parent() == o
    

async def test_get_event_from_type_node_BaseEvent(server):
    """
    This should work for following BaseEvent tests to work
    (maybe to write it a bit differentlly since they are not independent) 
    """
    ev = opcua.common.events.get_event_obj_from_type_node(
        opcua.Node(server.iserver.isession, ua.NodeId(ua.ObjectIds.BaseEventType))
    )
    check_base_event(ev)


async def test_get_event_from_type_node_Inhereted_AuditEvent(server):
    ev = opcua.common.events.get_event_obj_from_type_node(
        opcua.Node(server.iserver.isession, ua.NodeId(ua.ObjectIds.AuditEventType))
    )
    # we did not receive event
    assert ev is not None
    assert isinstance(ev, BaseEvent)
    assert isinstance(ev, AuditEvent)
    assert ev.EventType == ua.NodeId(ua.ObjectIds.AuditEventType)
    assert ev.Severity == 1
    assert ev.ActionTimeStamp is None
    assert ev.Status == False
    assert ev.ServerId is None
    assert ev.ClientAuditEntryId is None
    assert ev.ClientUserId is None


async def test_get_event_from_type_node_MultiInhereted_AuditOpenSecureChannelEvent(server):
    ev = opcua.common.events.get_event_obj_from_type_node(
        opcua.Node(server.iserver.isession, ua.NodeId(ua.ObjectIds.AuditOpenSecureChannelEventType))
    )
    assert ev is not None
    assert isinstance(ev, BaseEvent)
    assert isinstance(ev, AuditEvent)
    assert isinstance(ev, AuditSecurityEvent)
    assert isinstance(ev, AuditChannelEvent)
    assert isinstance(ev, AuditOpenSecureChannelEvent)
    assert ev.EventType == ua.NodeId(ua.ObjectIds.AuditOpenSecureChannelEventType)
    assert ev.Severity == 1
    assert ev.ClientCertificate is None
    assert ev.ClientCertificateThumbprint is None
    assert ev.RequestType is None
    assert ev.SecurityPolicyUri is None
    assert ev.SecurityMode is None
    assert ev.RequestedLifetime is None


async def test_eventgenerator_default(server):
    evgen = await server.get_event_generator()
    await check_eventgenerator_BaseEvent(evgen, server)
    await check_eventgenerator_SourceServer(evgen, server)


async def test_eventgenerator_BaseEvent_object(server):
    evgen = await server.get_event_generator(BaseEvent())
    await check_eventgenerator_BaseEvent(evgen, server)
    await check_eventgenerator_SourceServer(evgen, server)

"""
class TestServer(unittest.TestCase, CommonTests, SubscriptionTests, XmlTests):

    
    @classmethod
    def setUpClass(cls):
        cls.srv = Server()
        cls.srv.set_endpoint('opc.tcp://127.0.0.1:{0:d}'.format(port_num))
        add_server_methods(cls.srv)
        cls.srv.start()
        cls.opc = cls.srv
        cls.discovery = Server()
        cls.discovery.set_application_uri("urn:freeopcua:python:discovery")
        cls.discovery.set_endpoint('opc.tcp://127.0.0.1:{0:d}'.format(port_discovery))
        cls.discovery.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()
        cls.discovery.stop()

    # def test_register_server2(self):
        # servers = server.register_server()

    def test_eventgenerator_BaseEvent_Node(self):
        evgen = server.get_event_generator(opcua.Node(server.iserver.isession, ua.NodeId(ua.ObjectIds.BaseEventType)))
        check_eventgenerator_BaseEvent(self, evgen)
        check_eventgenerator_SourceServer(self, evgen)

    def test_eventgenerator_BaseEvent_NodeId(self):
        evgen = server.get_event_generator(ua.NodeId(ua.ObjectIds.BaseEventType))
        check_eventgenerator_BaseEvent(self, evgen)
        check_eventgenerator_SourceServer(self, evgen)

    def test_eventgenerator_BaseEvent_ObjectIds(self):
        evgen = server.get_event_generator(ua.ObjectIds.BaseEventType)
        check_eventgenerator_BaseEvent(self, evgen)
        check_eventgenerator_SourceServer(self, evgen)

    def test_eventgenerator_BaseEvent_Identifier(self):
        evgen = server.get_event_generator(2041)
        check_eventgenerator_BaseEvent(self, evgen)
        check_eventgenerator_SourceServer(self, evgen)

    def test_eventgenerator_sourceServer_Node(self):
        evgen = server.get_event_generator(source=opcua.Node(server.iserver.isession, ua.NodeId(ua.ObjectIds.Server)))
        check_eventgenerator_BaseEvent(self, evgen)
        check_eventgenerator_SourceServer(self, evgen)

    def test_eventgenerator_sourceServer_NodeId(self):
        evgen = server.get_event_generator(source=ua.NodeId(ua.ObjectIds.Server))
        check_eventgenerator_BaseEvent(self, evgen)
        check_eventgenerator_SourceServer(self, evgen)

    def test_eventgenerator_sourceServer_ObjectIds(self):
        evgen = server.get_event_generator(source=ua.ObjectIds.Server)
        check_eventgenerator_BaseEvent(self, evgen)
        check_eventgenerator_SourceServer(self, evgen)

    def test_eventgenerator_sourceMyObject(self):
        objects = server.get_objects_node()
        o = objects.add_object(3, 'MyObject')
        evgen = server.get_event_generator(source=o)
        check_eventgenerator_BaseEvent(self, evgen)
        check_event_generator_object(self, evgen, o)

    def test_eventgenerator_source_collision(self):
        objects = server.get_objects_node()
        o = objects.add_object(3, 'MyObject')
        event = BaseEvent(sourcenode=o.nodeid)
        evgen = server.get_event_generator(event, ua.ObjectIds.Server)
        check_eventgenerator_BaseEvent(self, evgen)
        check_event_generator_object(self, evgen, o)

    def test_eventgenerator_InheritedEvent(self):
        evgen = server.get_event_generator(ua.ObjectIds.AuditEventType)
        check_eventgenerator_SourceServer(self, evgen)

        ev = evgen.event
        self.assertIsNot(ev, None)  # we did not receive event
        self.assertIsInstance(ev, BaseEvent)
        self.assertIsInstance(ev, AuditEvent)
        self.assertEqual(ev.EventType, ua.NodeId(ua.ObjectIds.AuditEventType))
        self.assertEqual(ev.Severity, 1)
        self.assertEqual(ev.ActionTimeStamp, None)
        self.assertEqual(ev.Status, False)
        self.assertEqual(ev.ServerId, None)
        self.assertEqual(ev.ClientAuditEntryId, None)
        self.assertEqual(ev.ClientUserId, None)

    def test_eventgenerator_MultiInheritedEvent(self):
        evgen = server.get_event_generator(ua.ObjectIds.AuditOpenSecureChannelEventType)
        check_eventgenerator_SourceServer(self, evgen)

        ev = evgen.event
        self.assertIsNot(ev, None)  # we did not receive event
        self.assertIsInstance(ev, BaseEvent)
        self.assertIsInstance(ev, AuditEvent)
        self.assertIsInstance(ev, AuditSecurityEvent)
        self.assertIsInstance(ev, AuditChannelEvent)
        self.assertIsInstance(ev, AuditOpenSecureChannelEvent)
        self.assertEqual(ev.EventType, ua.NodeId(ua.ObjectIds.AuditOpenSecureChannelEventType))
        self.assertEqual(ev.Severity, 1),
        self.assertEqual(ev.ClientCertificate, None)
        self.assertEqual(ev.ClientCertificateThumbprint, None)
        self.assertEqual(ev.RequestType, None)
        self.assertEqual(ev.SecurityPolicyUri, None)
        self.assertEqual(ev.SecurityMode, None)
        self.assertEqual(ev.RequestedLifetime, None)

    # For the custom events all posibilites are tested. For other custom types only one test case is done since they are using the same code
    def test_create_custom_data_type_ObjectId(self):
        type = server.create_custom_data_type(2, 'MyDataType', ua.ObjectIds.BaseDataType, [('PropertyNum', ua.VariantType.Int32), ('PropertyString', ua.VariantType.String)])
        check_custom_type(self, type, ua.ObjectIds.BaseDataType)

    def test_create_custom_event_type_ObjectId(self):
        type = server.create_custom_event_type(2, 'MyEvent', ua.ObjectIds.BaseEventType, [('PropertyNum', ua.VariantType.Int32), ('PropertyString', ua.VariantType.String)])
        check_custom_type(self, type, ua.ObjectIds.BaseEventType)

    def test_create_custom_object_type_ObjectId(self):
        def func(parent, variant):
            return [ua.Variant(ret, ua.VariantType.Boolean)]

        properties = [('PropertyNum', ua.VariantType.Int32),
                      ('PropertyString', ua.VariantType.String)]
        variables = [('VariableString', ua.VariantType.String),
                     ('MyEnumVar', ua.VariantType.Int32, ua.NodeId(ua.ObjectIds.ApplicationType))]
        methods = [('MyMethod', func, [ua.VariantType.Int64], [ua.VariantType.Boolean])]

        node_type = server.create_custom_object_type(2, 'MyObjectType', ua.ObjectIds.BaseObjectType, properties, variables, methods)

        check_custom_type(self, node_type, ua.ObjectIds.BaseObjectType)
        variables = node_type.get_variables()
        self.assertTrue(node_type.get_child("2:VariableString") in variables)
        self.assertEqual(node_type.get_child("2:VariableString").get_data_value().Value.VariantType, ua.VariantType.String)
        self.assertTrue(node_type.get_child("2:MyEnumVar") in variables)
        self.assertEqual(node_type.get_child("2:MyEnumVar").get_data_value().Value.VariantType, ua.VariantType.Int32)
        self.assertEqual(node_type.get_child("2:MyEnumVar").get_data_type(), ua.NodeId(ua.ObjectIds.ApplicationType))
        methods = node_type.get_methods()
        self.assertTrue(node_type.get_child("2:MyMethod") in methods)

    # def test_create_custom_refrence_type_ObjectId(self):
        # type = server.create_custom_reference_type(2, 'MyEvent', ua.ObjectIds.Base, [('PropertyNum', ua.VariantType.Int32), ('PropertyString', ua.VariantType.String)])
        # check_custom_type(self, type, ua.ObjectIds.BaseObjectType)

    def test_create_custom_variable_type_ObjectId(self):
        type = server.create_custom_variable_type(2, 'MyVariableType', ua.ObjectIds.BaseVariableType, [('PropertyNum', ua.VariantType.Int32), ('PropertyString', ua.VariantType.String)])
        check_custom_type(self, type, ua.ObjectIds.BaseVariableType)

    def test_create_custom_event_type_NodeId(self):
        etype = server.create_custom_event_type(2, 'MyEvent', ua.NodeId(ua.ObjectIds.BaseEventType), [('PropertyNum', ua.VariantType.Int32), ('PropertyString', ua.VariantType.String)])
        check_custom_type(self, etype, ua.ObjectIds.BaseEventType)

    def test_create_custom_event_type_Node(self):
        etype = server.create_custom_event_type(2, 'MyEvent', opcua.Node(server.iserver.isession, ua.NodeId(ua.ObjectIds.BaseEventType)), [('PropertyNum', ua.VariantType.Int32), ('PropertyString', ua.VariantType.String)])
        check_custom_type(self, etype, ua.ObjectIds.BaseEventType)

    def test_get_event_from_type_node_CustomEvent(self):
        etype = server.create_custom_event_type(2, 'MyEvent', ua.ObjectIds.BaseEventType, [('PropertyNum', ua.VariantType.Int32), ('PropertyString', ua.VariantType.String)])

        ev = opcua.common.events.get_event_obj_from_type_node(etype)
        check_custom_event(self, ev, etype)
        self.assertEqual(ev.PropertyNum, 0)
        self.assertEqual(ev.PropertyString, None)

    def test_eventgenerator_customEvent(self):
        etype = server.create_custom_event_type(2, 'MyEvent', ua.ObjectIds.BaseEventType, [('PropertyNum', ua.VariantType.Int32), ('PropertyString', ua.VariantType.String)])

        evgen = server.get_event_generator(etype, ua.ObjectIds.Server)
        check_eventgenerator_CustomEvent(self, evgen, etype)
        check_eventgenerator_SourceServer(self, evgen)

        self.assertEqual(evgen.event.PropertyNum, 0)
        self.assertEqual(evgen.event.PropertyString, None)

    def test_eventgenerator_double_customEvent(self):
        event1 = server.create_custom_event_type(3, 'MyEvent1', ua.ObjectIds.BaseEventType, [('PropertyNum', ua.VariantType.Int32), ('PropertyString', ua.VariantType.String)])

        event2 = server.create_custom_event_type(4, 'MyEvent2', event1, [('PropertyBool', ua.VariantType.Boolean), ('PropertyInt', ua.VariantType.Int32)])

        evgen = server.get_event_generator(event2, ua.ObjectIds.Server)
        check_eventgenerator_CustomEvent(self, evgen, event2)
        check_eventgenerator_SourceServer(self, evgen)

        # Properties from MyEvent1
        self.assertEqual(evgen.event.PropertyNum, 0)
        self.assertEqual(evgen.event.PropertyString, None)

         # Properties from MyEvent2
        self.assertEqual(evgen.event.PropertyBool, False)
        self.assertEqual(evgen.event.PropertyInt, 0)

    def test_eventgenerator_customEvent_MyObject(self):
        objects = server.get_objects_node()
        o = objects.add_object(3, 'MyObject')
        etype = server.create_custom_event_type(2, 'MyEvent', ua.ObjectIds.BaseEventType, [('PropertyNum', ua.VariantType.Int32), ('PropertyString', ua.VariantType.String)])

        evgen = server.get_event_generator(etype, o)
        check_eventgenerator_CustomEvent(self, evgen, etype)
        check_event_generator_object(self, evgen, o)

        self.assertEqual(evgen.event.PropertyNum, 0)
        self.assertEqual(evgen.event.PropertyString, None)

    def test_context_manager(self):
        # Context manager calls start() and stop()
        state = [0]
        def increment_state(self, *args, **kwargs):
            state[0] += 1

        # create server and replace instance methods with dummy methods
        server = Server()
        server.start = increment_state.__get__(server)
        server.stop = increment_state.__get__(server)

        assert state[0] == 0
        with server:
            # test if server started
            self.assertEqual(state[0], 1)
        # test if server stopped
        self.assertEqual(state[0], 2)

    def test_get_node_by_ns(self):

        def get_ns_of_nodes(nodes):
            ns_list = set()
            for node in nodes:
                ns_list.add(node.nodeid.NamespaceIndex)
            return ns_list

        # incase other testss created nodes  in unregistered namespace
        _idx_d = server.register_namespace('dummy1')
        _idx_d = server.register_namespace('dummy2')
        _idx_d = server.register_namespace('dummy3')

        # create the test namespaces and vars
        idx_a = server.register_namespace('a')
        idx_b = server.register_namespace('b')
        idx_c = server.register_namespace('c')
        o = server.get_objects_node()
        _myvar2 = o.add_variable(idx_a, "MyBoolVar2", True)
        _myvar3 = o.add_variable(idx_b, "MyBoolVar3", True)
        _myvar4 = o.add_variable(idx_c, "MyBoolVar4", True)

        # the tests
        nodes = ua_utils.get_nodes_of_namespace(server, namespaces=[idx_a, idx_b, idx_c])
        self.assertEqual(len(nodes), 3)
        self.assertEqual(get_ns_of_nodes(nodes), set([idx_a, idx_b, idx_c]))

        nodes = ua_utils.get_nodes_of_namespace(server, namespaces=[idx_a])
        self.assertEqual(len(nodes), 1)
        self.assertEqual(get_ns_of_nodes(nodes), set([idx_a]))

        nodes = ua_utils.get_nodes_of_namespace(server, namespaces=[idx_b])
        self.assertEqual(len(nodes), 1)
        self.assertEqual(get_ns_of_nodes(nodes), set([idx_b]))

        nodes = ua_utils.get_nodes_of_namespace(server, namespaces=['a'])
        self.assertEqual(len(nodes), 1)
        self.assertEqual(get_ns_of_nodes(nodes), set([idx_a]))

        nodes = ua_utils.get_nodes_of_namespace(server, namespaces=['a', 'c'])
        self.assertEqual(len(nodes), 2)
        self.assertEqual(get_ns_of_nodes(nodes), set([idx_a, idx_c]))

        nodes = ua_utils.get_nodes_of_namespace(server, namespaces='b')
        self.assertEqual(len(nodes), 1)
        self.assertEqual(get_ns_of_nodes(nodes), set([idx_b]))

        nodes = ua_utils.get_nodes_of_namespace(server, namespaces=idx_b)
        self.assertEqual(len(nodes), 1)
        self.assertEqual(get_ns_of_nodes(nodes), set([idx_b]))

        self.assertRaises(ValueError, ua_utils.get_nodes_of_namespace, server, namespaces='non_existing_ns')
"""


async def check_eventgenerator_SourceServer(evgen, server: Server):
    server_node = server.get_server_node()
    assert evgen.event.SourceName == (await server_node.get_browse_name()).Name
    assert evgen.event.SourceNode == ua.NodeId(ua.ObjectIds.Server)
    assert await server_node.get_event_notifier() == {ua.EventNotifier.SubscribeToEvents}
    refs = await server_node.get_referenced_nodes(ua.ObjectIds.GeneratesEvent, ua.BrowseDirection.Forward,
                                                  ua.NodeClass.ObjectType, False)
    assert len(refs) >= 1


async def check_event_generator_object(evgen, obj):
    assert evgen.event.SourceName == obj.get_browse_name().Name
    assert evgen.event.SourceNode == obj.nodeid
    assert await obj.get_event_notifier() == {ua.EventNotifier.SubscribeToEvents}
    refs = await obj.get_referenced_nodes(ua.ObjectIds.GeneratesEvent, ua.BrowseDirection.Forward,
                                          ua.NodeClass.ObjectType, False)
    assert len(refs) == 1
    assert refs[0].nodeid == evgen.event.EventType


async def check_eventgenerator_BaseEvent(evgen, server: Server):
    # we did not receive event generator
    assert evgen is not None
    assert evgen.isession is server.iserver.isession
    check_base_event(evgen.event)


def check_base_event(ev):
    # we did not receive event
    assert ev is not None
    assert isinstance(ev, BaseEvent)
    assert ev.EventType == ua.NodeId(ua.ObjectIds.BaseEventType)
    assert ev.Severity == 1


def check_eventgenerator_CustomEvent(evgen, etype, server: Server):
    # we did not receive event generator
    assert evgen is not None
    assert evgen.isession is server.iserver.isession
    check_custom_event(evgen.event, etype)


def check_custom_event(ev, etype):
    # we did not receive event
    assert ev is not None
    assert isinstance(ev, BaseEvent)
    assert ev.EventType == etype.nodeid
    assert ev.Severity == 1


async def check_custom_type(type, base_type, server: Server):
    base = opcua.Node(server.iserver.isession, ua.NodeId(base_type))
    assert type in await base.get_children()
    nodes = await type.get_referenced_nodes(refs=ua.ObjectIds.HasSubtype, direction=ua.BrowseDirection.Inverse,
                                            includesubtypes=True)
    assert base == nodes[0]
    properties = type.get_properties()
    assert properties is not None
    assert len(properties) == 2
    assert type.get_child("2:PropertyNum") in properties
    assert type.get_child("2:PropertyNum").get_data_value().Value.VariantType == ua.VariantType.Int32
    assert type.get_child("2:PropertyString") in properties
    assert type.get_child("2:PropertyString").get_data_value().Value.VariantType == ua.VariantType.String

"""
class TestServerCaching(unittest.TestCase):
    def runTest(self):
        return # FIXME broken
        tmpfile = NamedTemporaryFile()
        path = tmpfile.name
        tmpfile.close()

        # create cache file
        server = Server(shelffile=path)

        # modify cache content
        id = ua.NodeId(ua.ObjectIds.Server_ServerStatus_SecondsTillShutdown)
        s = shelve.open(path, "w", writeback=True)
        s[id.to_string()].attributes[ua.AttributeIds.Value].value = ua.DataValue(123)
        s.close()

        # ensure that we are actually loading from the cache
        server = Server(shelffile=path)
        self.assertEqual(server.get_node(id).get_value(), 123)

        os.remove(path)

class TestServerStartError(unittest.TestCase):

    def test_port_in_use(self):

        server1 = Server()
        server1.set_endpoint('opc.tcp://127.0.0.1:{0:d}'.format(port_num + 1))
        server1.start()

        server2 = Server()
        server2.set_endpoint('opc.tcp://127.0.0.1:{0:d}'.format(port_num + 1))
        try:
            server2.start()
        except Exception:
            pass

        server1.stop()
        server2.stop()
"""