"""Loaded by the offline runner: even accidental real network calls fail closed."""
import socket

import pytest


@pytest.fixture(autouse=True)
def deny_external_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("Offline acceptance run forbids real network access")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
