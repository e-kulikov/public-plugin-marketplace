import unittest

from helpers import make_conv, msg, write
from chatgpt_backup import render as R


def info_ok(fid):
    return "ok", None, fid + "__x.png"


def info_missing(fid):
    return "unavailable", "file not found on server (404)", None


class Clean(unittest.TestCase):
    def test_markers(self):
        t = ("see citeturn0search0 and urlSitehttps://x.test/, "
             "entity[\"id1\",\"Atma\"] endmemcite ")
        self.assertEqual(R.clean_text(t), "see  and [Site](https://x.test/), Atma end")

    def test_unknown_marker_dropped(self):
        self.assertEqual(R.clean_text("aweirdzzzb"), "ab")


class Markdown(unittest.TestCase):
    def render(self, conv, info=info_ok, **meta):
        m = {"id": "cid", "project": None, "archived": False}
        m.update(meta)
        return R.render_markdown(conv, m, info)

    def test_basic_and_count(self):
        text, n = self.render(make_conv("c"))
        self.assertEqual(n, 2)
        self.assertIn("# Hello", text)
        self.assertEqual(text.count("<!-- msg role="), 2)
        self.assertEqual(n, R.expected_message_count(make_conv("c")))

    def test_voice_transcription_and_hidden_system(self):
        conv = make_conv("c", messages=[
            msg("system", ["secret"]),
            msg("user", [{"content_type": "audio_transcription", "text": " hello by voice"}], ctype="multimodal_text"),
            msg("assistant", ["ok"], is_visually_hidden_from_conversation=True),
            msg("assistant", ["visible"])])
        text, n = self.render(conv)
        self.assertEqual(n, 2)
        self.assertIn("hello by voice", text)
        self.assertNotIn("secret", text)
        self.assertEqual(R.expected_message_count(conv), 2)

    def test_image_ok_and_unavailable(self):
        part = {"content_type": "image_asset_pointer", "asset_pointer": "sediment://file_abc"}
        conv = make_conv("c", messages=[msg("user", [part, "look"], ctype="multimodal_text")])
        self.assertIn("![image](artifacts/file_abc__x.png)", self.render(conv)[0])
        self.assertIn("image unavailable: file not found on server (404)", self.render(conv, info_missing)[0])

    def test_attachment_link(self):
        conv = make_conv("c", messages=[msg("user", ["see file"], attachments=[{"id": "file-1", "name": "a.pdf"}])])
        self.assertIn("Attachment: [a.pdf](artifacts/file-1__x.png)", self.render(conv)[0])

    def test_tool_image_rendered_as_generated(self):
        part = {"content_type": "image_asset_pointer", "asset_pointer": "sediment://file_gen"}
        conv = make_conv("c", messages=[msg("user", ["draw"]), msg("tool", [part], ctype="multimodal_text")])
        text, n = self.render(conv)
        self.assertIn("### Generated image", text)
        self.assertEqual(n, 1)   # tool messages are not counted as dialogue

    def test_branch_note_and_flags(self):
        extra = {"alt": {"id": "alt", "parent": "root", "children": [], "message": msg("assistant", ["other branch"])}}
        text, _ = self.render(make_conv("c", extra_nodes=extra), archived=True, deleted=True, project="Proj")
        self.assertIn("Branches: 1 messages are on other branches", text)
        self.assertIn("Archived: yes", text)
        self.assertIn("Deleted on server: yes", text)
        self.assertIn("Project: Proj", text)
        self.assertNotIn("other branch\n", text.split("---", 1)[1])

    def test_code_and_output_in_details(self):
        conv = make_conv("c", messages=[
            msg("user", ["run"]),
            {"author": {"role": "assistant"}, "create_time": 1.0, "metadata": {},
             "content": {"content_type": "code", "language": "python", "text": "print(1)"}},
            {"author": {"role": "tool"}, "create_time": 1.0, "metadata": {},
             "content": {"content_type": "execution_output", "text": "1"}}])
        text, n = self.render(conv)
        self.assertIn("```python\nprint(1)\n```", text)
        self.assertEqual(n, 1)


class Files(unittest.TestCase):
    def test_refs_and_reasons(self):
        part = {"content_type": "image_asset_pointer", "asset_pointer": "sediment://file_abc"}
        aud = {"content_type": "audio_asset_pointer", "asset_pointer": "sediment://file_aud"}
        prev = {"content_type": "image_asset_pointer", "asset_pointer": "sediment://deadbeef#file_0123abcd#p_2.jpg"}
        conv = make_conv("c", messages=[msg("user", [part, aud, prev], ctype="multimodal_text",
                                            attachments=[{"id": "file-X", "name": "n.txt", "mime_type": "text/plain"}])])
        refs = R.file_refs(conv)
        self.assertEqual(set(refs), {"file_abc", "file_aud", "deadbeef#file_0123abcd#p_2.jpg", "file-X"})
        self.assertIsNone(R.unfetchable_reason("file_abc", "image"))
        self.assertIn("audio", R.unfetchable_reason("file_aud", "audio"))
        self.assertIn("page 3 of", R.unfetchable_reason("deadbeef#file_0123abcd#p_2.jpg", "image").replace("(page 3)", "page 3 of"))
        self.assertIn("not a downloadable", R.unfetchable_reason("weird", "image"))

    def test_download_name_and_sniff(self):
        self.assertEqual(R.download_basename("file_1", "my doc.pdf", None), "file_1__my_doc.pdf")
        self.assertEqual(R.download_basename("file_1", None, "image/png"), "file_1__.png")
        import tempfile, os
        with tempfile.TemporaryDirectory() as d:
            for name, data, ext in (("a", b"\x89PNG\r\n\x1a\n", ".png"), ("b", b"\xff\xd8\xff\xe0", ".jpg"),
                                    ("c", b"%PDF-1.4", ".pdf"), ("d", b"zzzz", ".bin")):
                p = os.path.join(d, name)
                write(p, data + b"\0" * 16)
                self.assertEqual(R.sniff_ext(p), ext)


if __name__ == "__main__":
    unittest.main()
