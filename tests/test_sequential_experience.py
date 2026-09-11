import json
from pathlib import Path
import tempfile
import unittest

from pqo.sequential_experience import _completed_episode_ids, _prepare_resume


class SequentialExperienceTests(unittest.TestCase):
    def test_resume_ignores_partial_behavior_trajectory(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "experience.jsonl"
            path.write_text(
                "\n".join(
                    json.dumps(record)
                    for record in (
                        {"episode_id": "partial", "behavior_done": False},
                        {"episode_id": "complete", "behavior_done": False},
                        {"episode_id": "complete", "behavior_done": True},
                    )
                )
                + "\n",
                encoding="utf-8",
            )

            self.assertEqual(_completed_episode_ids(path), {"complete"})
            self.assertEqual(_prepare_resume(path), {"complete"})
            remaining = [
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertTrue(remaining)
            self.assertTrue(
                all(record["episode_id"] == "complete" for record in remaining)
            )


if __name__ == "__main__":
    unittest.main()
