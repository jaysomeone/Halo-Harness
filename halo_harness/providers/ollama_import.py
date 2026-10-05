"""halo_harness.providers.ollama_import -- Halo 2.0.3 round 5c (brief item
4, "use it, option B"): `halo local import <model>` copies a `.gguf` file
into Ollama's own blob store and calls `providers.ollama.create_model`
on the chosen host, after a plain sentence that says the file is copied
into Ollama's store and how large it is. The result is an ordinary
`ol:<name>` with every one of rounds 2/3/5b's tuning already applied
(nothing import-specific needed there -- it is just another catalog
entry once `/api/tags` sees it).

FIX PASS (live run, build 0.34.2): the Modelfile/`modelfile` form this
module originally used is obsolete -- `POST /api/create` now answers
`HTTP 400 {"error":"neither 'from' or 'files' was specified"}` for it.
The current protocol, confirmed live: compute the file's own sha256,
`HEAD /api/blobs/sha256:<hex>` (404 when Ollama doesn't have it yet),
`POST /api/blobs/sha256:<hex>` with the raw bytes (201), then `POST
/api/create {"model": name, "files": {"<basename>": "sha256:<hex>"},
"stream": true}`. `build_modelfile` is kept only as the file's own
`FROM <path>` TEXT for display/logging -- it is never sent to Ollama
anymore; `providers.ollama.create_model`/`check_blob_exists`/
`upload_blob` do the real work.

GGUF only: the brief's own instruction is "list [importable safetensors
architectures] from the Ollama import docs you can read in docs/harness/
LOCAL-MODELS-RESEARCH.md; if that list is not there, implement GGUF only
and say so" -- that research doc's section 8 (Hugging Face, local) and its
"Documentation inconsistencies"/"What could not be confirmed" sections
were both read for this round and neither names which architectures
Ollama's own import path accepts for a raw safetensors folder. The list
is NOT there, so this module refuses a safetensors/MLX path outright with
a plain sentence pointing at option A instead, rather than guessing which
architectures would actually work.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Optional


def build_modelfile(gguf_path) -> str:
    """The file's own `FROM <absolute path>` text -- display/logging
    only since the fix pass (see module docstring); never sent to
    Ollama, which now identifies the file by its uploaded blob digest."""
    return f"FROM {Path(gguf_path).resolve()}\n"


def sha256_file(path) -> str:
    """Streamed, chunked sha256 (never reads the whole file into memory
    -- these are commonly hundreds of MB to tens of GB)."""
    hasher = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def import_consent_sentence(gguf_path, *, size_bytes: int, name: str) -> str:
    """Brief item 4: "a plain sentence that says the file will be copied
    into Ollama's store and how large it is" -- pinned verbatim by tests,
    so its wording is deliberately simple and stable."""
    from halo_harness.providers.ollama_panel import human_bytes
    return (f"Halo will copy {Path(gguf_path).name} ({human_bytes(size_bytes)}) into Ollama's own model "
            f"store as {name!r} (ollama create, via /api/create) -- the original file is left where it is.")


def refuse_non_gguf_sentence(fmt: str) -> str:
    return (f"halo local import only copies .gguf files into Ollama today -- Ollama's documented list of "
            f"safetensors architectures it can import directly was not found in this round's research, so a "
            f"{fmt} model is not offered this path; `halo local serve` (option A, a managed runtime) is the "
            f"answer for it instead.")


def import_gguf_to_ollama(host, name: str, gguf_path, *, on_status=None,
                           on_upload_progress=None) -> "tuple[bool, str]":
    """sha256 -> `HEAD`/`POST /api/blobs/*` (skipped when Ollama already
    has this exact blob) -> `POST /api/create` with `files` -- `(False,
    reason)` when `gguf_path` doesn't exist or isn't named `.gguf` (the
    caller's own `refuse_non_gguf_sentence` is for a FORMAT it already
    knows is wrong, e.g. from `providers.local_model_dirs`; this is the
    last-line defence against a bare path typo), or when the upload
    itself fails. Never raises -- every `providers.ollama.*` call here
    already degrades a transport failure to `(False, message)`."""
    path = Path(gguf_path)
    if not path.is_file():
        return False, f"not a file: {path}"
    if path.suffix.lower() != ".gguf":
        return False, refuse_non_gguf_sentence(path.suffix.lstrip(".") or "unknown")
    from halo_harness.providers.ollama import check_blob_exists, create_model, upload_blob
    digest_hex = sha256_file(path)
    already_present = check_blob_exists(host, digest_hex)
    if not already_present:  # False (confirmed absent) or None (unreachable -- try anyway)
        ok, message = upload_blob(host, digest_hex, path, on_progress=on_upload_progress)
        if not ok:
            return False, f"blob upload failed: {message}"
    files = {path.name: f"sha256:{digest_hex}"}
    return create_model(host, name, files=files, on_status=on_status)


def default_import_name(gguf_path) -> str:
    """A reasonable default `ol:<name>` -- the file's own stem, lowercased
    and with spaces/underscores folded to `-` (Ollama model names are
    conventionally lowercase, dash-separated). `halo local import --name`
    overrides this outright; this is only ever the no-`--name` default."""
    stem = Path(gguf_path).stem
    return "-".join(part for part in stem.lower().replace("_", "-").split() if part) or "imported-model"
