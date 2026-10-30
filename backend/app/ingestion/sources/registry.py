from pathlib import Path

import yaml
from pydantic import ValidationError

from app.ingestion.domain.errors import RegistryError
from app.ingestion.domain.models import SourceSpec


def load_registry(sources_file: Path) -> list[SourceSpec]:
    """Load and validate the approved-source registry."""
    try:
        raw = yaml.safe_load(sources_file.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RegistryError(f"source registry not found: {sources_file}") from exc
    except yaml.YAMLError as exc:
        raise RegistryError(f"source registry is not valid YAML: {exc}") from exc

    entries = (raw or {}).get("sources")
    if not isinstance(entries, list) or not entries:
        raise RegistryError(f"{sources_file} must contain a non-empty 'sources' list")

    specs: list[SourceSpec] = []
    for i, entry in enumerate(entries):
        try:
            specs.append(SourceSpec.model_validate(entry))
        except ValidationError as exc:
            raise RegistryError(f"invalid source entry #{i + 1}: {exc}") from exc

    ids = [s.id for s in specs]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        raise RegistryError(f"duplicate source ids: {duplicates}")
    for spec in specs:
        if spec.superseded_by and spec.superseded_by not in ids:
            raise RegistryError(f"{spec.id}: superseded_by refers to unknown source {spec.superseded_by!r}")
        if spec.superseded_by and spec.enabled:
            raise RegistryError(f"{spec.id}: a superseded source must have enabled: false")
    return specs
