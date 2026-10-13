import hashlib
import json
import os
import shutil
import time
from pathlib import Path


SKIP = {"target", ".git", ".ocswarm", "node_modules", "__pycache__", ".freebuff", ".opencode"}
BINARY = {".exe", ".dll", ".bundle", ".assets", ".ress", ".resource"}


def source_files(root):
	for folder, dirs, files in os.walk(root):
		dirs[:] = [d for d in dirs if d not in SKIP and not (Path(folder) / d).is_symlink()]
		for name in files:
			path = Path(folder) / name
			if path.is_symlink() or name.startswith(".env") or path.suffix.lower() in BINARY:
				continue
			if path.stat().st_size <= 8 * 1024 * 1024:
				yield path


def digest(path):
	if not path.is_file():
		return None
	hash = hashlib.sha256()
	with path.open("rb") as handle:
		for chunk in iter(lambda: handle.read(1024 * 1024), b""):
			hash.update(chunk)
	return hash.hexdigest()


def matches(path, owns):
	return any(path == own or path.startswith(own.rstrip("/") + "/") for own in owns)


class Workspace:
	def __init__(self, project, path, workspace_root, owns):
		self.project = Path(project).resolve()
		self.path = Path(path).resolve()
		self.owns = owns
		boundary = Path(workspace_root).resolve()
		if self.path == boundary or not self.path.is_relative_to(boundary):
			raise ValueError("The workspace path is outside its root.")
		for own in owns:
			resolved = (self.project / own).resolve()
			if not resolved.is_relative_to(self.project):
				raise ValueError("The owned path is outside the project.")
		self.previous_path = None
		if self.path.exists():
			archive = self.path.with_name(self.path.name + "-previous-" + str(time.time_ns()))
			if not archive.is_relative_to(boundary):
				raise ValueError("The archived workspace is outside its root.")
			self.path.rename(archive)
			self.previous_path = archive
		self.path.mkdir(parents=True)
		trees = [self.project / "etg-rs", self.project / "rea-analysis"] if (self.project / "etg-rs").is_dir() else [self.project]
		for tree in trees:
			if not tree.exists():
				continue
			for source in source_files(tree):
				target = self.path / source.relative_to(self.project)
				target.parent.mkdir(parents=True, exist_ok=True)
				shutil.copy2(source, target)
		self.baseline = {p.relative_to(self.path).as_posix(): digest(p) for p in source_files(self.path)}
		self.owned_baseline = {rel: value for rel, value in self.baseline.items() if matches(rel, owns)}
		for own in owns:
			if (self.project / own).is_file() and own not in self.owned_baseline:
				raise ValueError("The owned file is too large or is a binary.")
		index = self.path / ".ocswarm" / "source-index.json"
		index.parent.mkdir(parents=True, exist_ok=True)
		index.write_text(json.dumps(self.baseline), encoding="utf-8")

	def restore_candidate(self, candidate, workspace_root):
		candidate = Path(candidate).resolve()
		boundary = Path(workspace_root).resolve()
		if candidate == boundary or not candidate.is_relative_to(boundary):
			raise ValueError("The candidate workspace is outside its root.")
		index = json.loads((candidate / ".ocswarm" / "source-index.json").read_text(encoding="utf-8"))
		current = {p.relative_to(candidate).as_posix(): digest(p) for p in source_files(candidate)}
		changed = {rel for rel in current.keys() | index.keys() if current.get(rel) != index.get(rel)}
		if any(not matches(rel, self.owns) for rel in changed):
			raise ValueError("The saved candidate has edits outside owned paths.")
		for rel in set(index) | changed:
			if matches(rel, self.owns) and digest(self.project / rel) != index.get(rel):
				raise ValueError("The project changed after the saved candidate: " + rel)
		for rel in changed:
			target = self.path / rel
			if not target.resolve().is_relative_to(self.path):
				raise ValueError("A saved candidate path is outside its workspace.")
			source = candidate / rel
			if source.exists():
				target.parent.mkdir(parents=True, exist_ok=True)
				shutil.copy2(source, target)
			elif target.exists():
				target.unlink()

	def changes(self):
		current = {p.relative_to(self.path).as_posix(): digest(p) for p in source_files(self.path)}
		changed = {rel for rel in current.keys() | self.baseline.keys() if current.get(rel) != self.baseline.get(rel)}
		strays = sorted(rel for rel in changed if not matches(rel, self.owns))
		owned = sorted(rel for rel in changed if matches(rel, self.owns))
		return owned, strays

	def merge(self):
		changed, strays = self.changes()
		if strays:
			raise ValueError("Edits outside owned paths: " + ", ".join(strays[:8]))
		if not changed:
			raise ValueError("The owned files did not change.")
		for rel in set(self.owned_baseline) | set(changed):
			if digest(self.project / rel) != self.owned_baseline.get(rel):
				raise ValueError("The project changed during the task: " + rel)
		self.backup = self.path / ".ocswarm" / "merge-backup"
		self.applied = {rel: digest(self.path / rel) for rel in changed}
		for rel in changed:
			target = self.project / rel
			if target.exists():
				backup = self.backup / rel
				backup.parent.mkdir(parents=True, exist_ok=True)
				shutil.copy2(target, backup)
		self.transaction = self.backup / "transaction.json"
		self.transaction.parent.mkdir(parents=True, exist_ok=True)
		self.transaction.write_text(json.dumps({"phase": "prepared", "files": self.applied, "before": {rel: self.owned_baseline.get(rel) for rel in changed}}), encoding="utf-8")
		for rel in changed:
			target = self.project / rel
			source = self.path / rel
			if digest(target) != self.owned_baseline.get(rel):
				raise OSError("The project changed before merge: " + rel)
			if source.exists():
				target.parent.mkdir(parents=True, exist_ok=True)
				tmp = target.with_name(target.name + ".ocswarm-tmp")
				shutil.copy2(source, tmp)
				tmp.replace(target)
			else:
				target.unlink()
		return changed

	def commit(self, metadata=None):
		data = json.loads(self.transaction.read_text(encoding="utf-8"))
		data["phase"] = "accepted"
		data["metadata"] = metadata or {}
		tmp = self.transaction.with_suffix(".tmp")
		tmp.write_text(json.dumps(data), encoding="utf-8")
		tmp.replace(self.transaction)

	def rollback(self):
		conflicts = []
		for rel, value in self.applied.items():
			target = self.project / rel
			if digest(target) != value:
				conflicts.append(rel)
				continue
			backup = self.backup / rel
			if backup.exists():
				shutil.copy2(backup, target)
			elif target.exists():
				target.unlink()
		return conflicts

	@staticmethod
	def recover(project, workspace):
		project = Path(project).resolve()
		backup = Path(workspace) / ".ocswarm" / "merge-backup"
		manifest = backup / "transaction.json"
		if not manifest.exists():
			return None, []
		data = json.loads(manifest.read_text(encoding="utf-8"))
		if data["phase"] == "accepted":
			return data.get("metadata", {}), []
		conflicts = []
		for rel, expected in data["files"].items():
			target = (project / rel).resolve()
			if not target.is_relative_to(project):
				raise ValueError("A recovery path is outside the project.")
			current = digest(target)
			if current == data["before"].get(rel):
				continue
			if current != expected:
				conflicts.append(rel)
				continue
			original = backup / rel
			if original.exists():
				shutil.copy2(original, target)
			elif target.exists():
				target.unlink()
		return None, conflicts
