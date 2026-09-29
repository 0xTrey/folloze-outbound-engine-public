import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from connectors.apollo_client import ApolloClient  # noqa: E402


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeSession:
    def __init__(self, payload):
        self._payload = payload

    def post(self, *args, **kwargs):
        return _FakeResponse(self._payload)


def test_reveal_emails_ignores_null_bulk_match_entries():
    client = ApolloClient.__new__(ApolloClient)
    client.session = _FakeSession(
        {
            "matches": [
                None,
                {"id": "apollo-1", "email": "one@example.com"},
                {"email": "missing-id@example.com"},
            ]
        }
    )

    result = client.reveal_emails(["apollo-1", "apollo-2"])

    assert result == {
        "apollo-1": {"id": "apollo-1", "email": "one@example.com"},
    }
