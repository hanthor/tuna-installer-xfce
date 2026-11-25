"""Tests for .github/scripts/update-index.py.

The script reads a local OCI layout and rewrites a Flatpak `index/static`
file. It runs in the publish workflow, so a regression there only shows up
after a release is already built. These tests build tiny OCI layouts in a
temp dir and run the script as a subprocess, the way the workflow does.

Run from the repository root:  python3 -m unittest discover -s tests -v
"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / ".github" / "scripts" / "update-index.py"

REF = "org.flatpak.ref"
METADATA = "org.flatpak.metadata"


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


def make_oci(root, labels=None, architecture=None, os_=None, manifests=True):
    """Write a minimal OCI layout under root; return the manifest digest."""
    labels = {REF: "app/org.tunaos.InstallerXfce/x86_64/stable", METADATA: "[Application]"} if labels is None else labels
    config = {"config": {"Labels": labels}}
    if architecture is not None:
        config["architecture"] = architecture
    if os_ is not None:
        config["os"] = os_
    write_json(root / "blobs" / "sha256" / "cfg", config)
    write_json(root / "blobs" / "sha256" / "man", {"config": {"digest": "sha256:cfg"}})
    write_json(
        root / "index.json",
        {"manifests": [{"digest": "sha256:man"}] if manifests else []},
    )
    return "sha256:man"


class UpdateIndexTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.oci = self.tmp / "oci"
        self.index = self.tmp / "index" / "static"

    def run_script(self, *extra, repo="tuna-os/tavern"):
        return subprocess.run(
            [sys.executable, "-I", str(SCRIPT), "--oci-dir", str(self.oci),
             "--index-file", str(self.index), "--repo-name", repo, *extra],
            capture_output=True, text=True,
        )

    def load(self):
        return json.loads(self.index.read_text())

    def test_creates_index_with_defaults(self):
        digest = make_oci(self.oci, architecture="arm64", os_="linux")
        res = self.run_script()
        self.assertEqual(res.returncode, 0, res.stderr)
        data = self.load()
        self.assertEqual(data["Registry"], "https://ghcr.io")
        self.assertEqual(len(data["Results"]), 1)
        result = data["Results"][0]
        self.assertEqual(result["Name"], "tuna-os/tavern")
        image = result["Images"][0]
        self.assertEqual(image["Digest"], digest)
        self.assertEqual(image["MediaType"], "application/vnd.oci.image.manifest.v1+json")
        self.assertEqual(image["Architecture"], "arm64")
        self.assertEqual(image["OS"], "linux")
        self.assertEqual(image["Tags"], ["latest"])
        self.assertIn("arm64", res.stdout)
        self.assertTrue(self.index.read_text().endswith("\n"))

    def test_architecture_and_os_default_when_config_omits_them(self):
        make_oci(self.oci)
        self.assertEqual(self.run_script().returncode, 0)
        image = self.load()["Results"][0]["Images"][0]
        self.assertEqual(image["Architecture"], "amd64")
        self.assertEqual(image["OS"], "linux")

    def test_only_flatpak_labels_are_kept(self):
        make_oci(self.oci, labels={REF: "r", METADATA: "m", "org.opencontainers.image.title": "x", "other": "y"})
        self.assertEqual(self.run_script().returncode, 0)
        labels = self.load()["Results"][0]["Images"][0]["Labels"]
        self.assertEqual(labels, {REF: "r", METADATA: "m"})

    def test_custom_registry_and_tags(self):
        make_oci(self.oci)
        res = self.run_script("--registry", "registry.example.com", "--tags", "stable", "v1")
        self.assertEqual(res.returncode, 0, res.stderr)
        data = self.load()
        self.assertEqual(data["Registry"], "https://registry.example.com")
        self.assertEqual(data["Results"][0]["Images"][0]["Tags"], ["stable", "v1"])

    def test_same_architecture_is_replaced_other_architectures_kept(self):
        write_json(self.index, {"Registry": "https://ghcr.io", "Results": [{
            "Name": "tuna-os/tavern",
            "Images": [
                {"Digest": "sha256:old-amd", "Architecture": "amd64"},
                {"Digest": "sha256:old-arm", "Architecture": "arm64"},
            ],
        }]})
        digest = make_oci(self.oci, architecture="amd64")
        self.assertEqual(self.run_script().returncode, 0)
        images = self.load()["Results"][0]["Images"]
        by_arch = {i["Architecture"]: i["Digest"] for i in images}
        self.assertEqual(len(images), 2)
        self.assertEqual(by_arch, {"arm64": "sha256:old-arm", "amd64": digest})

    def test_new_repo_is_appended_without_touching_existing(self):
        existing = {"Name": "tuna-os/other", "Images": [{"Digest": "sha256:z", "Architecture": "amd64"}]}
        write_json(self.index, {"Registry": "https://ghcr.io", "Results": [existing]})
        make_oci(self.oci)
        self.assertEqual(self.run_script().returncode, 0)
        results = self.load()["Results"]
        self.assertEqual([r["Name"] for r in results], ["tuna-os/other", "tuna-os/tavern"])
        self.assertEqual(results[0], existing)

    def test_missing_index_json_fails(self):
        self.oci.mkdir()
        res = self.run_script()
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("index.json not found", res.stderr)
        self.assertFalse(self.index.exists())

    def test_empty_manifest_list_fails(self):
        make_oci(self.oci, manifests=False)
        res = self.run_script()
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("No manifests found", res.stderr)
        self.assertFalse(self.index.exists())

    def test_missing_required_label_fails_and_leaves_index_untouched(self):
        for missing in (REF, METADATA):
            with self.subTest(missing=missing):
                labels = {REF: "r", METADATA: "m"}
                del labels[missing]
                make_oci(self.oci, labels=labels)
                write_json(self.index, {"Registry": "https://ghcr.io", "Results": []})
                before = self.index.read_text()
                res = self.run_script()
                self.assertNotEqual(res.returncode, 0)
                self.assertIn(f"Missing required label: {missing}", res.stderr)
                self.assertEqual(self.index.read_text(), before)


if __name__ == "__main__":
    unittest.main()
