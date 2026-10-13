import httpx

from apps.importer.nexus import NexusClient


def test_public_anonymous_rpc_cannot_be_used_as_login():
    nexus = NexusClient('https://nexus.example', 'anonymous', 'wrong-password')
    nexus.client.close()
    nexus.client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={'result': {'userId': 'anonymous'}})))
    try:
        assert not nexus.authenticate()
    finally:
        nexus.close()
