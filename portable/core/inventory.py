"""core.inventory — read-only effective inventory of plugin/skill sources
(plan §8 Phase 3).

PURE by construction: no subprocess, no network, no writes — collect() only
reads files under the explicitly passed scoped roots (never a full-home scan).
Adapters are defensive file readers; a missing/malformed source yields
"unknown"/incomplete coverage, NEVER "absent" — hiding one adapter must degrade
to unknown, and a second adapter (e.g. the installed-plugins registry) can
still resolve identity.

Recorded per item: manifest/install name, source, scope, declared vs
loaded-observable distinction, evidence level, and notes. Stdlib only;
Python 3.8 compatible.
"""

import json
import re
from pathlib import Path

SOURCE_REGISTRY = "marketplace-installed"
SOURCE_CACHE = "cache"
SOURCE_SYNCED = "synced"
SOURCE_SKILLS_DIR = "skills-dir"
SOURCE_COMMANDS_DIR = "commands-dir"

EV_SOURCE_INSPECTED = "source-inspected"
EV_UNKNOWN = "unknown"

_FRONTMATTER_NAME = re.compile(r"^name:\s*(.+)$", re.MULTILINE)


def parse_version(v):
    """Dotted-numeric version -> tuple of ints; None when unparseable
    (an unknown version never wins a comparison)."""
    if not isinstance(v, str):
        return None
    parts = []
    for piece in v.strip().split("."):
        if not piece.isdigit():
            return None
        parts.append(int(piece))
    return tuple(parts) if parts else None


def _version_key(v):
    p = parse_version(v)
    if p is None:
        return (0,)
    width = max(len(p), 1)
    return p + (0,) * (width - len(p))


class Record:
    def __init__(self, name, source, kind, notes="", evidence_level=EV_SOURCE_INSPECTED):
        self.name = name
        self.source = source
        self.kind = kind
        self.notes = notes
        self.evidence_level = evidence_level
        self.scope = None
        self.installed_version = None
        self.declared_version = None
        self.loaded_observable = False

    def __repr__(self):
        return "Record(%r, source=%r, version=%r, loaded=%r)" % (
            self.name, self.source, self.installed_version, self.loaded_observable)


class Inventory:
    """Collect a read-only inventory under scoped roots.

    roots: {"claude_home": Path} — only these directories are read.
    """

    def __init__(self, roots):
        self.roots = {k: Path(v) for k, v in roots.items()}
        self.notes = []

    # ---------- adapters (each returns records; failures degrade to unknown) ----------

    def _read_registry(self, plugins_dir):
        reg = plugins_dir / "installed_plugins.json"
        if not reg.is_file():
            return {}, "registry-absent"
        try:
            data = json.loads(reg.read_text(encoding="utf-8-sig"))
        except Exception:
            return {}, "registry-malformed"
        if not isinstance(data, dict):
            return {}, "registry-malformed"
        out = {}
        for name, entry in data.items():
            if isinstance(entry, dict):
                out[str(name)] = {
                    "installPath": entry.get("installPath"),
                    "version": entry.get("version"),
                    "scope": entry.get("scope"),
                }
        return out, None

    def _scan_cache(self, plugins_dir):
        cache = plugins_dir / "cache"
        recs = []
        if not cache.is_dir():
            return recs, "cache-absent"
        for owner in sorted(cache.iterdir()):
            if not owner.is_dir():
                continue
            for plugin in sorted(owner.iterdir()):
                if not plugin.is_dir():
                    continue
                versions = []
                malformed = False
                for ver in sorted(plugin.iterdir()):
                    if not ver.is_dir():
                        continue
                    manifest = ver / ".claude-plugin" / "plugin.json"
                    if not manifest.is_file():
                        continue
                    try:
                        doc = json.loads(manifest.read_text(encoding="utf-8-sig"))
                        versions.append((str(doc.get("version", ver.name)), ver))
                    except Exception:
                        malformed = True
                name = plugin.name
                if versions:
                    best = max(versions, key=lambda t: _version_key(t[0]))
                    rec = Record(name, SOURCE_CACHE, "plugin")
                    rec.declared_version = best[0]
                    rec.loaded_observable = True
                    rec.notes = "cache"
                    recs.append(rec)
                if malformed:
                    rec = Record(name, SOURCE_CACHE, "plugin",
                                 notes="malformed manifest in cache",
                                 evidence_level=EV_UNKNOWN)
                    recs.append(rec)
        return recs, None

    def _scan_synced(self, plugins_dir, registry, cache_recs):
        synced = plugins_dir / "synced"
        if not synced.is_dir():
            self.notes.append("synced-absent")
            return []
        recs = []
        for child in sorted(synced.iterdir()):
            manifest = child / ".claude-plugin" / "plugin.json"
            if manifest.is_file():
                try:
                    doc = json.loads(manifest.read_text(encoding="utf-8-sig"))
                    # M4 (review): only claim shadowing when a same-name installed
                    # plugin actually exists — never assert it unconditionally
                    pname = str(doc.get("name", child.name))
                    shadowed = pname in registry or any(
                        r.name == pname for r in cache_recs)
                    rec = Record(pname, SOURCE_SYNCED, "plugin",
                                 notes=("synced copy is shadowed by same-name installed "
                                        "plugin (fact 4)" if shadowed else "synced copy; no "
                                        "same-name installed plugin observed"))
                    rec.declared_version = doc.get("version")
                    recs.append(rec)
                except Exception:
                    recs.append(Record(child.name, SOURCE_SYNCED, "plugin",
                                       notes="malformed manifest (synced)", evidence_level=EV_UNKNOWN))
        return recs

    def _scan_dir(self, directory, kind, source):
        recs = []
        if not directory.is_dir():
            return recs
        for child in sorted(directory.iterdir()):
            if kind == "loose-skill" and child.is_dir():
                skill_md = child / "SKILL.md"
                if not skill_md.is_file():
                    continue
                try:
                    text = skill_md.read_text(encoding="utf-8-sig")
                except Exception:
                    recs.append(Record(child.name, source, kind, notes="unreadable",
                                       evidence_level=EV_UNKNOWN))
                    continue
                m = _FRONTMATTER_NAME.search(text)
                recs.append(Record(m.group(1).strip() if m else child.name, source, kind))
            elif kind == "loose-command" and child.is_file() and child.suffix == ".md":
                recs.append(Record(child.stem, source, kind))
        return recs

    # ---------- collect ----------

    def collect(self):
        home = self.roots["claude_home"]
        plugins_dir = home / "plugins"
        merged = {}

        def merge(rec):
            prev = merged.get(rec.name)
            if prev is None:
                merged[rec.name] = rec
                return
            # merge: keep the better-observed record, accumulate notes
            if rec.loaded_observable and not prev.loaded_observable:
                rec.notes = (prev.notes + ";" + rec.notes).strip(";")
                if prev.installed_version or prev.declared_version:
                    rec.installed_version = prev.installed_version or prev.declared_version
                if prev.scope:
                    rec.scope = prev.scope
                merged[rec.name] = rec
            else:
                prev.notes = (prev.notes + ";" + rec.notes).strip(";")

        registry, reg_note = self._read_registry(plugins_dir)
        if reg_note:
            self.notes.append(reg_note)
        cache_recs, cache_note = self._scan_cache(plugins_dir)
        if cache_note:
            self.notes.append(cache_note)
        for rec in cache_recs:
            merge(rec)
        # registry entries: identity/scope from the registry; loaded-observable
        # only if the install path's manifest actually parses
        for name, entry in registry.items():
            rec = merged.get(name)
            if rec is None:
                rec = Record(name, SOURCE_REGISTRY, "plugin")
                merged[name] = rec
            install_path = entry.get("installPath")
            manifest_ok = False
            version = entry.get("version")
            if install_path:
                # never follow a registry-controlled path outside the declared
                # scoped roots (the registry is untrusted input)
                try:
                    Path(install_path).resolve().relative_to(home.resolve())
                    in_roots = True
                except ValueError:
                    in_roots = False
                if not in_roots:
                    rec.notes = (rec.notes + ";installPath-outside-roots (not read)").strip(";")
                    rec.loaded_observable = False
                else:
                    manifest = Path(install_path) / ".claude-plugin" / "plugin.json"
                    manifest_ok = manifest.is_file()
                    if manifest_ok:
                        try:
                            json.loads(manifest.read_text(encoding="utf-8-sig"))
                        except Exception:
                            manifest_ok = False
            rec.scope = entry.get("scope")
            if version:
                rec.installed_version = version
                if rec.declared_version is None:
                    rec.declared_version = version
            if manifest_ok:
                rec.loaded_observable = True
            elif install_path:
                rec.loaded_observable = False
                rec.notes = (rec.notes + ";cache-missing").strip(";")
                if rec.evidence_level != EV_UNKNOWN:
                    rec.evidence_level = EV_SOURCE_INSPECTED  # registry is authoritative for identity
        for rec in self._scan_synced(plugins_dir, registry, cache_recs):
            merge(rec)
        for rec in self._scan_dir(home / "skills", "loose-skill", SOURCE_SKILLS_DIR):
            merge(rec)
        for rec in self._scan_dir(home / "commands", "loose-command", SOURCE_COMMANDS_DIR):
            merge(rec)
        return [merged[k] for k in sorted(merged)]
