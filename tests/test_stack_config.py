import io
import unittest
import zipfile

from scripts.safety import Blocked
from scripts.stack_config import fix_disk_zip


def archive(source):
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as z:
        z.writestr('main.tf', source)
        z.writestr('other.txt', 'UNCHANGED')
    return output.getvalue()


class StackConfigTests(unittest.TestCase):
    def test_adds_only_boot_disk_attributes_and_preserves_other_entries(self):
        source = '''resource "oci_core_instance" "target" {
  metadata = { ssh_authorized_keys = var.ssh_public_key }
  source_details {
    source_id = var.image_id
    source_type = "image"
  }
}
'''
        result = fix_disk_zip(archive(source))
        with zipfile.ZipFile(io.BytesIO(result)) as z:
            text = z.read('main.tf').decode()
            self.assertIn('boot_volume_size_in_gbs = 50', text)
            self.assertIn('boot_volume_vpus_per_gb = 10', text)
            self.assertEqual(z.read('other.txt'), b'UNCHANGED')
            self.assertIn('metadata = { ssh_authorized_keys = var.ssh_public_key }', text)

    def test_replaces_only_simple_existing_disk_expressions(self):
        source = '''resource "oci_core_instance" "target" {
 source_details {
  source_type = "image"
  boot_volume_size_in_gbs = var.disk_size # preserve comment
  boot_volume_vpus_per_gb = null
 }
}
'''
        with zipfile.ZipFile(io.BytesIO(fix_disk_zip(archive(source)))) as z:
            text = z.read('main.tf').decode()
        self.assertEqual(text, source.replace('var.disk_size', '50').replace('= null', '= 10'))

    def test_refuses_ambiguous_or_complex_edits(self):
        for source in ('', 'resource "oci_core_instance" "a" {}\nresource "oci_core_instance" "b" {}',
                       'resource "oci_core_instance" "a" { dynamic "source_details" {} }',
                       'resource "oci_core_instance" "a" {\n source_details {\n boot_volume_size_in_gbs = lookup(var.sizes, "a", 50)\n }\n}'):
            with self.subTest(source=source), self.assertRaises(Blocked):
                fix_disk_zip(archive(source))
