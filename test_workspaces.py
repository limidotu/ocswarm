import tempfile
import unittest
from pathlib import Path

from swarm_workspace import Workspace


class WorkspaceTests(unittest.TestCase):
	def test_reusing_a_workspace_archives_the_previous_candidate(self):
		with tempfile.TemporaryDirectory() as tmp:
			base = Path(tmp)
			project = base / "project"
			project.mkdir()
			(project / "a.rs").write_text("old")
			old = Workspace(project, base / "work/one", base / "work", ["a.rs"])
			(old.path / "a.rs").write_text("candidate")
			new = Workspace(project, base / "work/one", base / "work", ["a.rs"])
			archives = list((base / "work").glob("one-previous-*"))
			self.assertEqual(len(archives), 1)
			self.assertEqual((archives[0] / "a.rs").read_text(), "candidate")
			self.assertEqual((new.path / "a.rs").read_text(), "old")
	def test_saved_candidate_resumes_and_keeps_new_unowned_changes(self):
		with tempfile.TemporaryDirectory() as tmp:
			base = Path(tmp)
			project = base / "project"
			project.mkdir()
			(project / "a.rs").write_text("old a")
			(project / "b.rs").write_text("old b")
			old = Workspace(project, base / "work/old", base / "work", ["a.rs"])
			(old.path / "a.rs").write_text("candidate a")
			(project / "b.rs").write_text("external b")
			new = Workspace(project, base / "work/new", base / "work", ["a.rs"])
			new.restore_candidate(old.path, base / "work")
			self.assertEqual((new.path / "a.rs").read_text(), "candidate a")
			self.assertEqual((new.path / "b.rs").read_text(), "external b")
			new.merge()
			self.assertEqual((project / "b.rs").read_text(), "external b")

	def test_saved_candidate_cannot_replace_a_changed_owned_file(self):
		with tempfile.TemporaryDirectory() as tmp:
			base = Path(tmp)
			project = base / "project"
			project.mkdir()
			(project / "a.rs").write_text("old")
			old = Workspace(project, base / "work/old", base / "work", ["a.rs"])
			(old.path / "a.rs").write_text("candidate")
			(project / "a.rs").write_text("external")
			new = Workspace(project, base / "work/new", base / "work", ["a.rs"])
			with self.assertRaisesRegex(ValueError, "project changed"):
				new.restore_candidate(old.path, base / "work")
			self.assertEqual((project / "a.rs").read_text(), "external")
	def test_parallel_workers_keep_each_others_changes(self):
		with tempfile.TemporaryDirectory() as tmp:
			base = Path(tmp)
			project = base / "project"
			project.mkdir()
			(project / "a.rs").write_text("old a")
			(project / "b.rs").write_text("old b")
			work = base / "work"
			a = Workspace(project, work / "a", work, ["a.rs"])
			b = Workspace(project, work / "b", work, ["b.rs"])
			(a.path / "a.rs").write_text("new a")
			(b.path / "b.rs").write_text("new b")
			a.merge()
			b.merge()
			self.assertEqual((project / "a.rs").read_text(), "new a")
			self.assertEqual((project / "b.rs").read_text(), "new b")
			b.rollback()
			self.assertEqual((project / "a.rs").read_text(), "new a")
			self.assertEqual((project / "b.rs").read_text(), "old b")

	def test_merge_does_not_replace_an_external_edit(self):
		with tempfile.TemporaryDirectory() as tmp:
			base = Path(tmp)
			project = base / "project"
			project.mkdir()
			(project / "a.rs").write_text("original")
			w = Workspace(project, base / "work" / "a", base / "work", ["a.rs"])
			(w.path / "a.rs").write_text("worker")
			(project / "a.rs").write_text("external")
			with self.assertRaisesRegex(ValueError, "project changed"):
				w.merge()
			self.assertEqual((project / "a.rs").read_text(), "external")

	def test_stray_edits_are_rejected_before_merge(self):
		with tempfile.TemporaryDirectory() as tmp:
			base = Path(tmp)
			project = base / "project"
			project.mkdir()
			(project / "a.rs").write_text("original")
			w = Workspace(project, base / "work" / "a", base / "work", ["a.rs"])
			(w.path / "a.rs").write_text("worker")
			(w.path / "OC_SWARM_JSON").write_text("result")
			with self.assertRaisesRegex(ValueError, "outside owned"):
				w.merge()
			self.assertEqual((project / "a.rs").read_text(), "original")

	def test_rollback_keeps_an_external_edit_after_merge(self):
		with tempfile.TemporaryDirectory() as tmp:
			base = Path(tmp)
			project = base / "project"
			project.mkdir()
			(project / "a.rs").write_text("original")
			w = Workspace(project, base / "work" / "a", base / "work", ["a.rs"])
			(w.path / "a.rs").write_text("worker")
			w.merge()
			(project / "a.rs").write_text("external")
			self.assertEqual(w.rollback(), ["a.rs"])
			self.assertEqual((project / "a.rs").read_text(), "external")

	def test_restart_recovers_an_unvalidated_merge(self):
		with tempfile.TemporaryDirectory() as tmp:
			base = Path(tmp)
			project = base / "project"
			project.mkdir()
			(project / "a.rs").write_text("original")
			w = Workspace(project, base / "work" / "a", base / "work", ["a.rs", "new.rs"])
			(w.path / "a.rs").write_text("candidate")
			(w.path / "new.rs").write_text("new")
			w.merge()
			result, conflicts = Workspace.recover(project, w.path)
			self.assertIsNone(result)
			self.assertFalse(conflicts)
			self.assertEqual((project / "a.rs").read_text(), "original")
			self.assertFalse((project / "new.rs").exists())

	def test_restart_keeps_an_accepted_merge(self):
		with tempfile.TemporaryDirectory() as tmp:
			base = Path(tmp)
			project = base / "project"
			project.mkdir()
			(project / "a.rs").write_text("original")
			w = Workspace(project, base / "work" / "a", base / "work", ["a.rs"])
			(w.path / "a.rs").write_text("accepted")
			w.merge()
			w.commit({"id": "task", "state": "done"})
			result, conflicts = Workspace.recover(project, w.path)
			self.assertEqual(result["state"], "done")
			self.assertFalse(conflicts)
			self.assertEqual((project / "a.rs").read_text(), "accepted")


if __name__ == "__main__":
	unittest.main()
