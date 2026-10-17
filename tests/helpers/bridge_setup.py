"""A provider outside MEDRE's namespace for SDK-free authoring tests."""

from dataclasses import dataclass, replace

from medre import adapter_registry
from medre.adapter_registry import AdapterTypeRegistry
from medre.adapter_spec import AdapterSample, SymbolRef
from medre.config import loader
from tests.helpers.adapter_spec import make_adapter_spec


@dataclass(frozen=True)
class ChatConfig:
    adapter_id: str
    origin_label: str = ""
    credential: str = ""
    unlock_phrase: str = ""
    store_path: str = ""
    allowed_contexts: tuple[str, ...] = ()

    def validate(self):
        return self


def sample():
    return AdapterSample({"origin_label": "Private chat"}, "opaque-room")


def install_provider(monkeypatch):
    spec = replace(
        make_adapter_spec("independent_chat"),
        config=SymbolRef(__name__, "ChatConfig"),
        fake_adapter=SymbolRef(
            "medre.adapters.fakes.transport", "FakeTransportAdapter"
        ),
        sample_config=SymbolRef(__name__, "sample"),
        support_secret_fields=("credential", "unlock_phrase"),
    )
    monkeypatch.setattr(
        adapter_registry, "ADAPTER_REGISTRY", AdapterTypeRegistry((spec,))
    )
    monkeypatch.setattr(loader, "_KNOWN_TRANSPORTS", frozenset((spec.transport,)))
    return spec


CONFIG = """\
runtime:
  name: private-bridge
storage:
  backend: memory
adapters:
  independent_chat:
    left:
      adapter_kind: fake
      origin_label: Chat A
      credential: keep-this-private
      unlock_phrase: provider-declared-sensitive-value
      store_path: '{state}/left'
    right:
      adapter_kind: fake
      origin_label: Chat B
routes: {}
"""
