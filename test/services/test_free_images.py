import io
import os
import shutil
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from app.config import config
from app.models.schema import VideoAspect
from app.services import free_images, material


def _png(width=1600, height=900, color=(120, 80, 40)):
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, format="PNG")
    return buffer.getvalue()


def _commons_page(index, title, license_id="pd", width=2000, height=1400, mime="image/jpeg", object_name=""):
    return {
        "index": index,
        "title": title,
        "imageinfo": [{
            "url": f"https://upload.example/{index}.jpg",
            "thumburl": f"https://upload.example/thumb/{index}.jpg",
            "descriptionurl": f"https://commons.wikimedia.org/wiki/{title}",
            "width": width,
            "height": height,
            "mime": mime,
            "extmetadata": {
                "License": {"value": license_id},
                "LicenseShortName": {"value": license_id},
                "ObjectName": {"value": object_name},
            },
        }],
    }


class FakeSession:
    def __init__(self, pages, image=b"img"):
        self.pages = pages
        self.image = image
        self.downloads = []

    def get(self, url, params=None, headers=None, timeout=None):
        assert headers and "EchoShorts" in headers["User-Agent"]
        if url == free_images.COMMONS_API:
            pages = {str(p["index"]): p for p in self.pages}
            return SimpleNamespace(json=lambda: {"query": {"pages": pages}})
        self.downloads.append(url)
        return SimpleNamespace(status_code=200, content=self.image)


class FreeImagesTests(unittest.TestCase):
    def setUp(self):
        self.original_app_config = dict(config.app)
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def tearDown(self):
        config.app.clear()
        config.app.update(self.original_app_config)

    def test_split_term(self):
        self.assertEqual(
            free_images.split_term("dancing plague 1518 :: a woman dancing on a cobbled street"),
            ("dancing plague 1518", "a woman dancing on a cobbled street"),
        )
        self.assertEqual(free_images.split_term("crowd dancing"), ("crowd dancing", "crowd dancing"))
        self.assertEqual(free_images.split_term(":: only prompt"), ("only prompt", "only prompt"))

    def test_commons_takes_first_free_large_unused_picture(self):
        session = FakeSession([
            _commons_page(1, "File:Dancing plague used.jpg"),
            _commons_page(2, "File:Dancing plague licensed.jpg", license_id="cc-by-sa-4.0"),
            _commons_page(3, "File:Dancing plague tiny.jpg", width=400, height=300),
            _commons_page(4, "File:Dancing plague.gif", mime="image/gif"),
            _commons_page(5, "File:King David and musicians.jpg"),
            # A crop of a work already shown counts as the same work.
            _commons_page(6, "File:Dancing plague 1518 detail.jpg"),
            # Relevance may come from the object name rather than the file name.
            _commons_page(7, "File:Brueghel.jpg", license_id="pd", object_name="The dancing plague"),
        ])
        used = {"File:Dancing plague used.jpg", free_images._work_key("File:Dancing plague 1518.jpg")}
        image, source = free_images.search_commons("dancing plague", used, session=session)
        self.assertEqual(image, b"img")
        self.assertEqual(source["asset_id"], "File:Brueghel.jpg")
        self.assertEqual(session.downloads, ["https://upload.example/thumb/7.jpg"])
        self.assertIn("File:Brueghel.jpg", used)
        self.assertIn(free_images._work_key("File:Brueghel (cropped).jpg"), used)
        # CC0 counts as free, attribution licences do not.
        self.assertTrue(free_images._is_free_license({"License": {"value": "cc0"}}))
        self.assertFalse(free_images._is_free_license({"License": {"value": "cc-by-4.0"}}))

    def test_relevance_needs_half_of_the_query_words(self):
        self.assertTrue(free_images._is_relevant("Strasbourg 1518 dance", "File:Danse Strasbourg 1518.jpg", {}))
        self.assertTrue(free_images._is_relevant("Saint Vitus", "File:Saint-Guy.jpg", {"ObjectName": {"value": "<b>Vitus</b>"}}))
        # Broad descriptions and categories do not count.
        self.assertFalse(free_images._is_relevant(
            "Saint Vitus shrine", "File:Saint Venceslas grave.JPG",
            {"ImageDescription": {"value": "Shrine in St. Vitus Cathedral"}, "Categories": {"value": "Shrines|Saint Vitus"}},
        ))
        self.assertFalse(free_images._is_relevant("medieval musicians bagpipe drum", "File:King David, wife Musica and musicians.jpg", {}))
        self.assertTrue(free_images._is_relevant("Écorché", "File:Ecorche.jpg", {}))

    def test_commons_returns_none_without_a_free_match(self):
        session = FakeSession([_commons_page(1, "File:A.jpg", license_id="cc-by-4.0")])
        self.assertIsNone(free_images.search_commons("x", set(), session=session))

    def test_local_command_fills_placeholders_per_argument(self):
        config.app["local_image_command"] = (
            "mflux-generate --prompt {prompt} --width {width} --height {height} --output {output}"
        )
        config.app["local_image_prompt_template"] = "photo of {term}"
        calls = []

        def fake_run(command, **kwargs):
            calls.append(command)
            with open(command[command.index("--output") + 1], "wb") as f:
                f.write(b"generated")
            return subprocess.CompletedProcess(command, 0, "", "")

        data = free_images.generate_local("a woman; rm -rf /", VideoAspect.portrait, self.tmp, run=fake_run)
        self.assertEqual(data, b"generated")
        command = calls[0]
        # The whole prompt is one argument: no shell, no injection.
        self.assertEqual(command[command.index("--prompt") + 1], "photo of a woman; rm -rf /")
        self.assertEqual(command[command.index("--width") + 1], "720")
        self.assertEqual(command[command.index("--height") + 1], "1280")
        self.assertEqual(os.listdir(self.tmp), [])

    def test_local_command_unset_or_failing_gives_none(self):
        config.app.pop("local_image_command", None)
        self.assertIsNone(free_images.generate_local("x", VideoAspect.portrait, self.tmp))
        config.app["local_image_command"] = "gen --output {output}"
        failed = lambda command, **kwargs: subprocess.CompletedProcess(command, 1, "", "boom")  # noqa: E731
        self.assertIsNone(free_images.generate_local("x", VideoAspect.portrait, self.tmp, run=failed))

    def test_landscape_painting_fills_most_of_the_portrait_canvas(self):
        canvas = free_images.frame_for_canvas(_png(1600, 900), VideoAspect.portrait)
        self.assertEqual(canvas.size, (1080, 1920))
        # The painting covers 60% of the height (sides cropped) instead of a
        # thin strip; the top edge shows the darker blurred fill.
        self.assertEqual(canvas.getpixel((540, 960)), (120, 80, 40))
        self.assertEqual(canvas.getpixel((540, 960 - 560)), (120, 80, 40))
        self.assertLess(sum(canvas.getpixel((540, 20))), sum((120, 80, 40)))
        # A portrait picture is never cropped: it is fitted whole.
        tall = free_images.frame_for_canvas(_png(900, 1600), VideoAspect.portrait)
        self.assertEqual(tall.getpixel((5, 960)), (120, 80, 40))
        with self.assertRaises(free_images.FreeImageError):
            free_images.frame_for_canvas(b"not an image", VideoAspect.portrait)

    def test_mixed_prefers_archive_then_falls_back_to_local(self):
        commons_calls, local_calls = [], []

        def commons(query, used):
            commons_calls.append(query)
            return (_png(), {"provider": "wikimedia_commons", "asset_id": "File:A.jpg",
                             "source_page": "https://commons.wikimedia.org/wiki/File:A.jpg"}) if query == "found" else None

        def local(prompt, aspect, out_dir):
            local_calls.append(prompt)
            return _png(720, 1280)

        archive = free_images.generate_images_mixed(
            "found :: a scene", 4, save_dir=self.tmp, commons=commons, local=local)
        generated = free_images.generate_images_mixed(
            "missing :: a woman dancing", 4, save_dir=self.tmp, commons=commons, local=local)

        self.assertEqual(archive[0].source_info["provider"], "wikimedia_commons")
        self.assertEqual(generated[0].source_info["provider"], "local_image")
        self.assertEqual(local_calls, ["a woman dancing"])
        for item in archive + generated:
            self.assertEqual(item.provider, "mixed_image")
            self.assertEqual(item.duration, 4)
            with Image.open(item.url) as saved:
                self.assertEqual(saved.size, (1080, 1920))

        nothing = free_images.generate_images_mixed(
            "missing :: x", 4, save_dir=self.tmp, commons=commons, local=lambda *a: None)
        self.assertEqual(nothing, [])

    def test_material_routes_mixed_source_through_the_image_loop(self):
        with patch.object(material, "_download_videos_openai_image_on_demand", return_value=["a.mp4"]) as loop:
            result = material.download_videos(
                task_id="t", search_terms=["q :: p"], source="mixed_image",
                audio_duration=10, max_clip_duration=4,
            )
        self.assertEqual(result, ["a.mp4"])
        generate = loop.call_args.kwargs["generate_images"]
        with patch.object(free_images, "generate_images_mixed", return_value=[]) as mixed:
            generate(search_term="q :: p", minimum_duration=4, video_aspect=VideoAspect.portrait, save_dir="")
        self.assertIsInstance(mixed.call_args.kwargs["used"], set)

    def test_stock_source_mixes_in_archive_pictures(self):
        stock_calls = []

        def stock(provider, search_videos, search_term, minimum_duration, video_aspect):
            stock_calls.append(search_term)
            return [SimpleNamespace(url=f"https://stock/{search_term}")]

        def archive(search_term, used, local, **kwargs):
            self.assertIsNone(local("prompt", VideoAspect.portrait, self.tmp))  # no local AI here
            if search_term != "Brueghel Molenbeek":
                return []
            item = SimpleNamespace(url=os.path.join(self.tmp, "a.jpg"), provider="mixed_image")
            return [item]

        captured = {}

        def by_order(**kwargs):
            search = kwargs["search_videos"]
            captured["plain"] = search("crowd dancing", 2, VideoAspect.portrait)
            captured["found"] = search("archive: Brueghel Molenbeek :: crowd dancing", 2, VideoAspect.portrait)
            captured["missing"] = search("Archive: Hondius 1642 :: candle flame dark", 2, VideoAspect.portrait)
            return []

        with patch.object(material, "_search_videos_with_cache", side_effect=stock), \
                patch.object(free_images, "generate_images_mixed", side_effect=archive), \
                patch.object(material.video, "render_image_zoom_video", return_value="/tmp/a.jpg.zoom-2.mp4"), \
                patch.object(material, "_download_videos_by_script_order", side_effect=by_order):
            material.download_videos(
                task_id="t", search_terms=["x"], source="pixabay",
                audio_duration=10, max_clip_duration=2, match_script_order=True,
            )

        self.assertEqual(captured["plain"][0].url, "https://stock/crowd dancing")
        self.assertEqual(captured["found"][0].url, "/tmp/a.jpg.zoom-2.mp4")
        # No archive picture: the stock fallback term is searched instead.
        self.assertEqual(captured["missing"][0].url, "https://stock/candle flame dark")
        self.assertEqual(stock_calls, ["crowd dancing", "candle flame dark"])

    def test_local_clip_needs_no_download(self):
        path = os.path.join(self.tmp, "clip.mp4")
        with open(path, "wb") as f:
            f.write(b"mp4")
        self.assertEqual(material.save_video(path), path)


if __name__ == "__main__":
    unittest.main()
