"""3단계: 사진 보정 — 회전·정사각형·자동 보정·압축·배경 제거·재처리 방지·원본 보존."""
import hashlib
import os
import random

import pytest
from PIL import Image, ImageDraw

from services.images import product_images
from services.images.processing import (
    ImageOptions, auto_enhance, load_image, on_white_centered, process_product, save_jpeg, to_square,
)


def solid(path, size, color, fmt=None, **kw):
    Image.new("RGB", size, color).save(path, fmt, **kw)
    return path


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def make_product(root, code, files):
    folder = root / code / "원본"
    folder.mkdir(parents=True)
    for name, size, color in files:
        solid(folder / name, size, color)
    return folder


# --- 단위 보정 ---
def test_load_image_exif_rotation_and_transparency(tmp_path):
    img = Image.new("RGB", (400, 200), (200, 50, 50))
    exif = img.getexif()
    exif[0x0112] = 6   # 90도 회전해서 보라는 표시
    img.save(tmp_path / "rot.jpg", exif=exif)
    assert load_image(tmp_path / "rot.jpg").size == (200, 400)

    rgba = Image.new("RGBA", (10, 10), (0, 0, 0, 0))
    rgba.save(tmp_path / "t.png")
    loaded = load_image(tmp_path / "t.png")
    assert loaded.mode == "RGB" and loaded.getpixel((5, 5)) == (255, 255, 255)


def test_to_square_pad_and_crop():
    img = Image.new("RGB", (400, 200), (200, 0, 0))
    padded = to_square(img, 1000, "pad")
    assert padded.size == (1000, 1000)
    assert padded.getpixel((500, 10)) == (255, 255, 255) and padded.getpixel((500, 500))[0] > 190
    cropped = to_square(img, 1000, "crop")
    assert cropped.size == (1000, 1000) and cropped.getpixel((500, 10))[0] > 190


def test_auto_enhance_brightens_dark_photo():
    dark = Image.new("RGB", (100, 100), (60, 58, 55))
    ImageDraw.Draw(dark).rectangle((0, 0, 30, 30), fill=(120, 118, 115))
    assert sum(auto_enhance(dark).getpixel((60, 60))) > sum(dark.getpixel((60, 60)))


def test_auto_enhance_fixes_tinted_white_wall():
    img = Image.new("RGB", (200, 200), (215, 225, 245))           # 푸르스름하게 찍힌 흰 벽
    ImageDraw.Draw(img).rectangle((60, 40, 140, 180), fill=(150, 60, 50))   # 상품
    wall = auto_enhance(img).getpixel((10, 10))
    assert max(wall) - min(wall) < 30 - 10                        # 벽의 색 기울기가 줄어듦


def test_auto_enhance_keeps_neutral_wall_with_colored_product():
    """회귀: 네이비 상품이 화면을 채워도 중성 회색 벽을 누렇게 물들이지 않는다."""
    img = Image.new("RGB", (400, 400), (254, 254, 254))
    ImageDraw.Draw(img).rectangle((200, 0, 400, 400), fill=(234, 234, 234))   # 오른쪽은 회색 벽
    ImageDraw.Draw(img).rectangle((80, 60, 320, 380), fill=(40, 55, 90))      # 네이비 셔츠
    out = auto_enhance(img)
    r, g, b = out.getpixel((380, 20))
    assert max(r, g, b) - min(r, g, b) <= 2                       # 여전히 무채색
    assert out.getpixel((20, 20)) == (254, 254, 254)               # 흰 벽 그대로
    nr, ng, nb = out.getpixel((200, 200))
    assert nb > nr                                                 # 상품 색도 그대로 푸른 계열


def test_save_jpeg_respects_size_limit(tmp_path):
    rnd = random.Random(1)
    noisy = Image.frombytes("RGB", (1000, 1000), bytes(rnd.getrandbits(8) for _ in range(3_000_000)))
    size = save_jpeg(noisy, tmp_path / "n.jpg", max_bytes=600_000)
    assert size == os.path.getsize(tmp_path / "n.jpg")
    assert size < save_jpeg(noisy, tmp_path / "n2.jpg", max_bytes=10_000_000)


def test_on_white_centered_crops_object():
    cut = Image.new("RGBA", (800, 600), (0, 0, 0, 0))
    ImageDraw.Draw(cut).rectangle((600, 400, 700, 550), fill=(10, 120, 10, 255))   # 구석에 있는 물체
    out = on_white_centered(cut, 1000)
    assert out.getpixel((5, 5)) == (255, 255, 255)
    assert out.getpixel((500, 500))[1] > 100       # 물체가 가운데로


# --- 상품 단위 처리 ---
def test_process_product_outputs_and_preserves_originals(tmp_path):
    folder = make_product(tmp_path, "A001", [("01.png", (800, 600), (180, 60, 60)), ("02.jpg", (600, 900), (60, 60, 180)),
                                             ("03_detail.png", (2000, 500), (240, 240, 240))])
    before = {p.name: sha(p) for p in folder.iterdir()}
    r = process_product(tmp_path, "A001", ImageOptions())
    assert r.errors == [] and len(r.processed) == 3
    out = tmp_path / "A001" / "보정"
    assert Image.open(out / "01.jpg").size == (1000, 1000) and Image.open(out / "02.jpg").size == (1000, 1000)
    assert Image.open(out / "03_detail.jpg").size == (1000, 250)              # 상세용은 비율 유지
    assert {p.name: sha(p) for p in folder.iterdir()} == before               # 원본 그대로
    images = product_images(tmp_path, "A001")
    assert images.edited and images.representative.name == "01.jpg" and images.detail[0].name == "03_detail.jpg"


def test_process_product_skips_already_processed(tmp_path):
    folder = make_product(tmp_path, "A001", [("01.png", (500, 500), (200, 0, 0)), ("02.png", (500, 500), (0, 200, 0))])
    process_product(tmp_path, "A001", ImageOptions())
    again = process_product(tmp_path, "A001", ImageOptions())
    assert again.processed == [] and again.skipped == 2

    solid(folder / "02.png", (500, 400), (0, 0, 200))                         # 원본이 바뀌면 그 사진만
    os.utime(folder / "02.png", (1, 1))
    changed = process_product(tmp_path, "A001", ImageOptions())
    assert [p.name for p in changed.processed] == ["02.jpg"] and changed.skipped == 1

    crop = process_product(tmp_path, "A001", ImageOptions(square_mode="crop"))  # 설정이 바뀌면 다시
    assert len(crop.processed) == 2

    (folder / "02.png").unlink()                                               # 원본이 없어지면 보정본 정리
    gone = process_product(tmp_path, "A001", ImageOptions(square_mode="crop"))
    assert gone.removed == ["02.jpg"] and not (tmp_path / "A001" / "보정" / "02.jpg").exists()


def test_background_removal_only_for_representative(tmp_path):
    make_product(tmp_path, "A001", [("01.png", (600, 600), (90, 90, 90)), ("02.png", (600, 600), (90, 90, 90))])
    calls = []

    def fake_remover(img):
        calls.append(img.size)
        cut = Image.new("RGBA", img.size, (0, 0, 0, 0))
        ImageDraw.Draw(cut).ellipse((200, 200, 400, 400), fill=(20, 20, 160, 255))
        return cut

    r = process_product(tmp_path, "A001", ImageOptions(remove_background=True), remover=fake_remover)
    assert r.background_removed and len(calls) == 1
    rep = Image.open(tmp_path / "A001" / "보정" / "01.jpg")
    assert rep.size == (1000, 1000) and min(rep.getpixel((10, 10))) > 245 and rep.getpixel((500, 500))[2] > 120
    other = Image.open(tmp_path / "A001" / "보정" / "02.jpg")
    assert max(other.getpixel((500, 500))) < 200                               # 추가 이미지는 배경 그대로

    # 배경 제거 실패해도 보정은 계속 (원래 배경 유지 + 경고)
    def broken(img):
        raise RuntimeError("모델 다운로드 실패")
    r = process_product(tmp_path, "A001", ImageOptions(remove_background=True, bg_model="other"), remover=broken)
    assert any("배경 제거 실패" in e for e in r.errors) and (tmp_path / "A001" / "보정" / "01.jpg").exists()


def test_name_collision_and_missing_originals(tmp_path):
    make_product(tmp_path, "A001", [("01.png", (300, 300), (255, 0, 0)), ("01.jpg", (300, 300), (0, 255, 0))])
    r = process_product(tmp_path, "A001", ImageOptions())
    assert sorted(p.name for p in r.processed) == ["01.jpg", "01_png.jpg"]
    assert process_product(tmp_path, "NONE", ImageOptions()).errors


def test_heic_original(tmp_path):
    pillow_heif = pytest.importorskip("pillow_heif")
    folder = tmp_path / "A001" / "원본"
    folder.mkdir(parents=True)
    try:
        pillow_heif.from_pillow(Image.new("RGB", (640, 480), (30, 160, 90))).save(folder / "IMG_0001.HEIC")
    except Exception as e:
        pytest.skip(f"HEIC 인코더 없음: {e}")
    images = product_images(tmp_path, "A001")
    assert images.found and not images.edited                                 # 보정 전에도 원본으로 인식
    r = process_product(tmp_path, "A001", ImageOptions())
    assert r.errors == [] and Image.open(tmp_path / "A001" / "보정" / "IMG_0001.jpg").size == (1000, 1000)


# --- 등록 흐름·CLI 연동 ---
from tests.test_automation_register import FakeClient, FakeServices, add_rows, env, row, sheet  # noqa: E402,F401


class UploadRecorder(FakeClient):
    def __init__(self):
        super().__init__()
        self.uploaded: list[str] = []

    def post(self, path, json=None, files=None):
        if path.endswith("/product-images/upload"):
            self.uploaded += [f[1][0] for f in files]
        return super().post(path, json=json, files=files)


def test_run_uploads_processed_images(env):
    from services.automation import runner
    db, root, excel = env
    folder = root / "img" / "A001" / "원본"
    folder.mkdir(parents=True)
    solid(folder / "01.png", (1600, 1200), (200, 80, 80))          # 큰 원본 → 보정본으로 업로드
    solid(folder / "02.bmp", (300, 900), (80, 80, 200))
    solid(folder / "03_detail.png", (900, 300), (250, 250, 250))
    add_rows(excel, [row("A001")])
    client = UploadRecorder()
    runner.run_waiting(FakeServices(db, client), dry_run=False)
    # 상세페이지용까지 한 번씩만 올리고, 상품 이미지는 그 주소를 재사용한다
    assert client.uploaded == ["01.jpg", "02.jpg", "03_detail.jpg"]
    images = client.registered[0]["originProduct"]["images"]
    assert images["representativeImage"]["url"] == "https://shop-phinf/0"
    assert [i["url"] for i in images["optionalImages"]] == ["https://shop-phinf/1"]
    assert (root / "img" / "A001" / "보정" / "01.jpg").exists()
    assert sheet(excel)["A001"][0] == "등록완료(판매중지)"


def test_dry_run_accepts_heic_without_processing(env):
    pillow_heif = pytest.importorskip("pillow_heif")
    from services.automation import runner
    db, root, excel = env
    folder = root / "img" / "A001" / "원본"
    folder.mkdir(parents=True)
    try:
        pillow_heif.from_pillow(Image.new("RGB", (640, 480), (30, 160, 90))).save(folder / "IMG_1.HEIC")
    except Exception as e:
        pytest.skip(f"HEIC 인코더 없음: {e}")
    add_rows(excel, [row("A001")])
    report = runner.run_waiting(FakeServices(db, FakeClient()), dry_run=True)
    assert report.ok_items and not (root / "img" / "A001" / "보정").exists()   # dry-run은 보정 파일을 만들지 않음


def test_cli_process_images(env, capsys):
    import cli
    db, root, excel = env
    for code in ("A001", "A002"):
        folder = root / "img" / code / "원본"
        folder.mkdir(parents=True)
        solid(folder / "01.png", (400, 300), (120, 120, 120))
    add_rows(excel, [row("A001"), row("A002", 상태="판매중")])
    assert cli.main(["process-images"]) == 0                       # 기본: 대기 상품만
    out = capsys.readouterr().out
    assert "A001  보정 1장" in out and "A002" not in out
    assert cli.main(["process-images", "--all"]) == 0
    out = capsys.readouterr().out
    assert "A001  보정 0장 · 건너뜀 1장" in out and "A002  보정 1장" in out
    assert cli.main(["process-images", "--code", "NONE"]) == 1


def test_drop_small_parts_removes_detached_shadow():
    from services.images.processing import drop_small_parts
    cut = Image.new("RGBA", (400, 400), (0, 0, 0, 0))
    d = ImageDraw.Draw(cut)
    d.rectangle((100, 50, 300, 300), fill=(200, 200, 200, 255))      # 옷
    d.ellipse((120, 340, 280, 360), fill=(90, 90, 90, 160))          # 떨어진 바닥 그림자 (작음)
    out = drop_small_parts(cut)
    assert out.getpixel((200, 350))[3] == 0 and out.getpixel((200, 150))[3] == 255
    assert out.split()[-1].getbbox()[3] <= 305                       # 확대 기준이 옷만

    pair = Image.new("RGBA", (400, 200), (0, 0, 0, 0))                # 비슷한 크기 두 덩어리(예: 신발 한 켤레)는 유지
    d = ImageDraw.Draw(pair)
    d.rectangle((20, 50, 170, 150), fill=(0, 0, 0, 255))
    d.rectangle((230, 50, 380, 150), fill=(0, 0, 0, 255))
    out = drop_small_parts(pair)
    assert out.getpixel((100, 100))[3] == 255 and out.getpixel((300, 100))[3] == 255


def test_fill_enclosed_white_shirt_but_keeps_open_gaps():
    from services.images.processing import drop_small_parts, fill_enclosed
    # 흰 옷을 밝은 배경에서 찍으면 모델이 윤곽선만 남긴다 → 둘러싸인 안쪽은 원래 색으로 채움
    cut = Image.new("RGBA", (400, 400), (245, 245, 240, 0))
    d = ImageDraw.Draw(cut)
    d.rectangle((100, 50, 300, 300), outline=(180, 180, 180, 255), width=4)   # 윤곽선만 남은 흰 옷
    d.line((60, 200, 96, 200), fill=(180, 180, 180, 255), width=4)            # 바깥과 이어진 틈 쪽 (소매)
    d.ellipse((110, 340, 290, 360), fill=(90, 90, 90, 255))                   # 바닥 그림자
    out = drop_small_parts(fill_enclosed(cut))
    assert out.getpixel((200, 150)) == (245, 245, 240, 255)                   # 안쪽: 원래 색 그대로, 불투명
    assert out.getpixel((80, 150))[3] == 0                                    # 바깥 틈은 지운 채로
    assert out.getpixel((200, 350))[3] == 0                                   # 이제 옷이 커서 그림자는 작은 덩어리
    assert fill_enclosed(out).tobytes() == out.tobytes()                      # 채울 곳이 없으면 그대로


def test_background_quality_setting_maps_to_model():
    from services.automation.config import BG_MODELS, AutomationSettings
    assert AutomationSettings().image_options().bg_model == BG_MODELS["high"] == "birefnet-general"
    assert AutomationSettings(bg_quality="normal").image_options().bg_model == "birefnet-general-lite"
    assert AutomationSettings(bg_quality="fast").image_options().bg_model == "silueta"
    assert AutomationSettings.from_dict({"bg_model": "silueta"}).bg_quality == "high"   # 옛 설정 값은 무시


def test_photos_directly_in_product_folder(tmp_path):
    folder = tmp_path / "TEST2"                                   # 원본/ 폴더 없이 바로 넣은 사진
    folder.mkdir()
    src = solid(folder / "phone.jpg", (800, 600), (200, 80, 80))
    before = sha(src)
    images = product_images(tmp_path, "TEST2")
    assert images.representative == src and not images.edited
    r = process_product(tmp_path, "TEST2", ImageOptions())
    assert [p.name for p in r.processed] == ["phone.jpg"] and (folder / "보정" / "phone.jpg").exists()
    assert sha(src) == before                                     # 원본은 그대로
    assert product_images(tmp_path, "TEST2").edited
    make_product(tmp_path, "A001", [("01.jpg", (600, 600), (10, 10, 10))])
    solid(tmp_path / "A001" / "stray.jpg", (600, 600), (0, 0, 0))   # 원본/에 사진이 있으면 원본/만 사용
    assert [p.name for p in process_product(tmp_path, "A001", ImageOptions()).processed] == ["01.jpg"]


def test_fill_enclosed_keeps_real_background_gaps():
    from services.images.processing import fill_enclosed
    # 소매가 치마에 닿아 생긴 큰 틈(실제 배경)은 채우지 않고, 아주 작은 구멍만 채운다
    cut = Image.new("RGBA", (400, 400), (250, 250, 250, 0))
    d = ImageDraw.Draw(cut)
    d.rectangle((50, 50, 350, 350), fill=(120, 70, 40, 255))   # 옷
    d.rectangle((80, 120, 140, 220), fill=(250, 250, 250, 0))   # 큰 틈 (둘러싸임)
    d.rectangle((200, 200, 202, 202), fill=(120, 70, 40, 0))    # 작은 구멍
    out = fill_enclosed(cut)
    assert out.getpixel((110, 170))[3] == 0 and out.getpixel((201, 201))[3] == 255


def test_defringe_removes_background_halo_and_feather_softens():
    from services.images.processing import clean_cutout, defringe, feather
    # 마스크가 옷보다 3px 넓게 잡혀 베이지 배경 띠가 남은 경우
    img = Image.new("RGBA", (500, 500), (233, 230, 225, 0))
    d = ImageDraw.Draw(img)
    d.rectangle((97, 97, 403, 403), fill=(233, 230, 225, 255))     # 배경색 띠 (불투명으로 잘못 잡힘)
    d.rectangle((100, 100, 400, 400), fill=(60, 60, 140, 255))      # 옷
    out = defringe(img)
    assert out.getpixel((98, 250))[3] == 0                          # 배경색 띠는 투명
    assert out.getpixel((101, 250))[3] == 255                       # 옷 가장자리는 그대로
    soft = feather(out).getpixel((100, 250))[3]
    assert 0 < soft < 255                                           # 경계가 살짝 부드러워짐
    assert clean_cutout(img).getpixel((250, 250)) == (60, 60, 140, 255)
