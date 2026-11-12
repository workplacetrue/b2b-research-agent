"""Owner-edited, versioned data. No executable configuration and no stale-cache fallback."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, ValidationError, model_validator

from .models import FrozenModel, ProofAsset, SenderProfile, ServiceOffer


class CatalogValidationError(ValueError):
    """A safe error identifying an owner input file and field, never credentials."""


class ServicesDocument(FrozenModel):
    schema_version: Literal[1] = 1
    services: tuple[ServiceOffer, ...] = ()


class ProofDocument(FrozenModel):
    schema_version: Literal[1] = 1
    public_case_status: Literal["available", "none_confirmed", "unknown"] = "unknown"
    assets: tuple[ProofAsset, ...] = ()
    status_source: str = "unknown"

    @model_validator(mode="after")
    def status_has_source(self):
        if self.public_case_status == "none_confirmed" and self.status_source == "unknown":
            raise ValueError("none_confirmed requires an explicit owner status_source")
        return self


def _canonical(value) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _hash(value) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


class CatalogSnapshot(FrozenModel):
    schema_version: Literal[1] = 1
    version: str
    services: tuple[ServiceOffer, ...] = ()
    proof_assets: tuple[ProofAsset, ...] = ()
    sender_profile: SenderProfile = Field(default_factory=SenderProfile)
    public_case_status: Literal["available", "none_confirmed", "unknown"] = "unknown"
    status_source: str = "unknown"

    def service(self, service_id: str) -> ServiceOffer:
        for offer in self.services:
            if offer.id == service_id:
                return offer
        raise CatalogValidationError(f"services.json: services.id: unknown service {service_id!r}")

    def proof(self, asset_id: str) -> ProofAsset:
        for asset in self.proof_assets:
            if asset.id == asset_id:
                return asset
        raise CatalogValidationError(f"proof_assets.json: assets.id: unknown asset {asset_id!r}")

    def dependency_version(self, service_id: str, proof_asset_ids=None) -> str:
        offer = self.service(service_id)
        used = offer.proof_asset_ids if proof_asset_ids is None else tuple(proof_asset_ids)
        assets = [self.proof(asset_id).model_dump(mode="json") for asset_id in sorted(set(used))]
        return _hash({
            "service": offer.model_dump(mode="json"),
            "proof_assets": assets,
            "sender_profile": self.sender_profile.model_dump(mode="json"),
            "public_case_status": self.public_case_status,
            "status_source": self.status_source,
        })

    def save(self, directory: str | Path) -> Path:
        """Keep immutable audit input, separate from owner-editable INPUT."""
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"{self.version}.json"
        if not target.exists():
            with target.open("x", encoding="utf-8") as handle:
                handle.write(self.model_dump_json(indent=2))
            target.chmod(0o600)
        return target


def _stamp(path: Path):
    value = path.stat()
    return value.st_ino, value.st_size, value.st_mtime_ns


def _read_document(path: Path, schema):
    try:
        before = _stamp(path)
        if before[1] > 4 * 1024 * 1024:
            raise CatalogValidationError(f"{path.name}: file exceeds 4 MiB")
        content = path.read_bytes()
        if before != _stamp(path):
            raise CatalogValidationError(f"{path.name}: file changed while reading; finish saving and retry")
        return schema.model_validate_json(content), before
    except CatalogValidationError:
        raise
    except (OSError, UnicodeError) as error:
        raise CatalogValidationError(f"{path.name}: {type(error).__name__}; check the file and permissions") from None
    except ValidationError as error:
        issues = []
        for detail in error.errors(include_input=False, include_url=False):
            location = ".".join(str(part) for part in detail["loc"]) or "document"
            issues.append(f"{location}: {detail['msg']}")
        raise CatalogValidationError(f"{path.name}: " + "; ".join(issues[:8])) from None


def _validate_links_and_paths(snapshot: CatalogSnapshot, root: Path):
    ids = [item.id for item in snapshot.services]
    asset_ids = [item.id for item in snapshot.proof_assets]
    if len(ids) != len(set(ids)):
        raise CatalogValidationError("services.json: services.id: duplicate IDs")
    if len(asset_ids) != len(set(asset_ids)):
        raise CatalogValidationError("proof_assets.json: assets.id: duplicate IDs")
    for offer in snapshot.services:
        for asset_id in offer.proof_asset_ids:
            if asset_id not in asset_ids:
                raise CatalogValidationError(f"services.json: services.{offer.id}.proof_asset_ids: unknown asset {asset_id}")
    for asset in snapshot.proof_assets:
        for service_id in asset.service_ids:
            if service_id not in ids:
                raise CatalogValidationError(f"proof_assets.json: assets.{asset.id}.service_ids: unknown service {service_id}")
        if asset.path:
            candidate = (root / asset.path).resolve()
            if not candidate.is_relative_to(root):
                raise CatalogValidationError(f"proof_assets.json: assets.{asset.id}.path: must stay inside INPUT")
            if asset.availability == "available" and not candidate.is_file():
                raise CatalogValidationError(f"proof_assets.json: assets.{asset.id}.path: available material is missing")
        if asset.url:
            parsed = urlsplit(asset.url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
                raise CatalogValidationError(f"proof_assets.json: assets.{asset.id}.url: expected an HTTP(S) URL without credentials")
        if (asset.publication_allowed or asset.attachment_allowed) and asset.availability != "available":
            raise CatalogValidationError(f"proof_assets.json: assets.{asset.id}: publication requires available material")
        if asset.attachment_allowed and not asset.path:
            raise CatalogValidationError(f"proof_assets.json: assets.{asset.id}.path: attachments require a local material")


def load_service_catalog(input_path: str | Path = "INPUT", *, snapshot_dir: str | Path | None = None) -> CatalogSnapshot:
    """Read all files consistently and validate; never silently reuse an old price."""
    root = Path(input_path).resolve()
    schemas = {"services.json": ServicesDocument, "proof_assets.json": ProofDocument, "sender_profile.json": SenderProfile}
    documents = {}
    stamps = {}
    for filename, schema in schemas.items():
        documents[filename], stamps[filename] = _read_document(root / filename, schema)
    for filename in schemas:
        try:
            unchanged = stamps[filename] == _stamp(root / filename)
        except OSError:
            unchanged = False
        if not unchanged:
            raise CatalogValidationError(f"{filename}: catalog changed while reading; finish saving and retry")
    services, proof, sender = (documents[name] for name in schemas)
    data = {
        "services": services.services,
        "proof_assets": proof.assets,
        "sender_profile": sender,
        "public_case_status": proof.public_case_status,
        "status_source": proof.status_source,
    }
    version = _hash({name: document.model_dump(mode="json") for name, document in documents.items()})
    snapshot = CatalogSnapshot(version=version, **data)
    _validate_links_and_paths(snapshot, root)
    if snapshot_dir is not None:
        snapshot.save(snapshot_dir)
    return snapshot


def with_legacy_sender_defaults(snapshot: CatalogSnapshot, config) -> CatalogSnapshot:
    """Hash the sender actually rendered when older configs supply missing fields."""
    sender = snapshot.sender_profile
    updates = {
        "brand": sender.brand or config.sender,
        "from_email": sender.from_email or config.outreach.from_email,
        "signature": sender.signature or config.outreach.signature,
    }
    if all(getattr(sender, key) == value for key, value in updates.items()):
        return snapshot
    effective_sender = sender.model_copy(update=updates)
    values = snapshot.model_dump(mode="json", exclude={"version"})
    values["sender_profile"] = effective_sender.model_dump(mode="json")
    return snapshot.model_copy(update={"sender_profile": effective_sender, "version": _hash(values)})


LEGACY_SERVICES = {
    "sales_intake": ("Обработка входящих заявок", ("обработка заявок", "приём заказов", "сбор заявок")),
    "sales_quotes": ("Подготовка коммерческих предложений", ("коммерческие предложения", "расчёт предложения", "подготовка КП")),
    "sales_followup": ("Контроль согласованных повторных продаж", ("повторные продажи", "контроль follow-up", "согласованные напоминания")),
    "documents": ("Подготовка и обработка документов", ("обработка документов", "подготовка документов", "сверка документов")),
}


def load_legacy_catalog(config) -> CatalogSnapshot:
    """Explicit compatible capability declarations; never client proof or known prices."""
    offers = []
    for workflow in config.outreach.workflows:
        if workflow not in LEGACY_SERVICES:
            continue
        name, tasks = LEGACY_SERVICES[workflow]
        offers.append(ServiceOffer(
            id=workflow, workflow=workflow, name=name, supported_tasks=tasks,
            limitations=("Объём и совместимость требуют проверки",),
            allowed_claims=({"text": f"Обсуждаем применимость сценария: {name}", "source": "legacy outreach.workflows", "kind": "capability_declaration"},),
        ))
    sender = SenderProfile(
        brand=config.sender, from_email=config.outreach.from_email,
        signature=config.outreach.signature, source="legacy config sender/signature",
    )
    contents = {
        "services": [offer.model_dump(mode="json") for offer in offers],
        "sender_profile": sender.model_dump(mode="json"),
    }
    return CatalogSnapshot(version=_hash(contents), services=tuple(offers), sender_profile=sender)


def initialize_input(input_path: str | Path = "INPUT", legacy_config=None) -> list[Path]:
    """Initialize missing owner files only. Existing owner content is never overwritten."""
    root = Path(input_path)
    root.mkdir(parents=True, exist_ok=True)
    template = Path(__file__).resolve().parents[2] / "examples" / "INPUT"
    snapshot = load_legacy_catalog(legacy_config) if legacy_config is not None else None
    fallback = {
        "services.json": {"schema_version": 1, "services": [offer.model_dump(mode="json") for offer in snapshot.services]} if snapshot else {"schema_version": 1, "services": []},
        "proof_assets.json": {"schema_version": 1, "public_case_status": "unknown", "status_source": "unknown", "assets": []},
        "sender_profile.json": snapshot.sender_profile.model_dump(mode="json") if snapshot else SenderProfile().model_dump(mode="json"),
        "README.md": "Редактируйте JSON, затем выполните b2b-agent validate-input. Пустой реестр кейсов означает unknown. Каждое сообщение требует отдельного разрешения.\n",
    }
    created = []
    for name, content in fallback.items():
        target = root / name
        if target.exists():
            continue
        try:
            with target.open("x", encoding="utf-8") as handle:
                if legacy_config is None and (template / name).is_file():
                    handle.write((template / name).read_text(encoding="utf-8"))
                elif name.endswith(".json"):
                    json.dump(content, handle, ensure_ascii=False, indent=2)
                    handle.write("\n")
                else:
                    handle.write(content)
        except FileExistsError:
            continue
        created.append(target)
    return created
