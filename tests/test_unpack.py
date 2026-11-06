"""Unpacking FromSoftware game archives (.bhd/.bdt) natively: the header's RSA and the files' AES,
the path hashes, file names from lists and by pattern, the unpack plan and run, and the engine's
commands for it."""

import os
from pathlib import Path

import pytest
import synth
from test_app import wait
from test_pipeline import weapon_dir  # noqa: F401 - fixture

from darkstl.app.config import Config
from darkstl.app.service import AppError, Service
from darkstl.importers.fromsoft import unpack
from darkstl.importers.fromsoft.formats import bhd5, bhd5_keys, dcx


@pytest.fixture(scope="module")
def keypair():
    return synth.rsa_keypair(1024)


def game_files(root: Path) -> list[tuple[str, bytes]]:
    out = []
    for dirpath, _dirs, names in os.walk(root):
        for name in sorted(names):
            p = Path(dirpath) / name
            out.append(("/" + p.relative_to(root).as_posix(), p.read_bytes()))
    return out


def pack_game(folder: Path, files, archives: dict[str, list[str]], fmt: str, keypair=None, ext=".bhd",
              aes=True) -> None:
    """Write ``files`` into archive pairs in ``folder``: ``archives`` maps an archive name to the
    path prefixes it holds; with ``keypair`` headers are RSA-encrypted."""
    folder.mkdir(parents=True, exist_ok=True)
    for name, prefixes in archives.items():
        chosen = [(p, d) for p, d in files if p.lower().startswith(tuple(prefixes))]
        head, data = synth.build_bhd5(chosen, fmt, aes=aes)
        if keypair is not None:
            n, _e, d, _pem = keypair
            head = bhd5.encrypt_header_for_tests(head, n, d)
        (folder / f"{name}{ext}").write_bytes(head)
        (folder / f"{name}.bdt").write_bytes(data)


@pytest.fixture
def packed_ds3(tmp_path, weapon_dir, keypair, monkeypatch):  # noqa: F811
    """The synthetic Dark Souls III game of test_pipeline, packed the way the game ships: in
    Data1-Data2 with encrypted headers (our own key standing in for the game's)."""
    files = [(p, d) for p, d in game_files(weapon_dir) if p.endswith(".dcx")]
    files.append(("/sfx/frpg_sfxbnd_common.ffxbnd.dcx", b"effects: not unpacked"))
    files.append(("/chr/c1100.behbnd.dcx", b"behaviour: not unpacked"))
    other = dcx.decompress((weapon_dir / "parts" / "wp_a_0300.partsbnd.dcx").read_bytes())[0]
    other = other.replace(b"WP_A_0300", b"WP_A_0301").replace("WP_A_0300".encode("utf-16-le"), "WP_A_0301".encode("utf-16-le"))
    files.append(("/parts/wp_a_0301.partsbnd.dcx", dcx.compress_dflt(other)))  # named by guess only
    # a file whose hash a guessed name hits, which isn't that model: it stays unnamed
    files.append(("/parts/wp_a_7777.partsbnd.dcx", dcx.compress_dflt(b"BND4" + b"\0" * 60)))
    game = tmp_path / "install" / "DARK SOULS III" / "Game"
    pack_game(game, files, {"Data1": ["/parts/"], "Data2": ["/chr/", "/sfx/"]}, "DS3", keypair)
    (game / "DarkSoulsIII.exe").write_bytes(b"")
    (game / "oo2core_6_win64.dll").write_bytes(b"not really")
    pem_b64 = "".join(keypair[3].splitlines()[1:-1])
    monkeypatch.setattr(unpack, "key_for", lambda g, a: pem_b64 if g == "Dark Souls III" else None)
    lists = tmp_path / "lists"
    lists.mkdir()
    (lists / "DarkSouls3Dictionary.txt").write_text(
        "#data1\n/parts/wp_a_0300.partsbnd.dcx\n/parts/hd_m_1000.partsbnd.dcx\n/chr/c1100.chrbnd.dcx\n"
        "/chr/c1100.texbnd.dcx\n/chr/c1100.behbnd.dcx\n/sfx/frpg_sfxbnd_common.ffxbnd.dcx\n")
    monkeypatch.setattr(unpack, "bundled_lists", lambda spec: [])  # only the list above: guesses must do the rest
    unpack._names_from.cache_clear()
    return game, lists


def test_path_hashes_follow_the_games():
    # the fold, by hand: lower case, forward slashes, a leading slash
    h = 0
    for c in "/chr/c2010.chrbnd.dcx":
        h = (h * 37 + ord(c)) & 0xFFFFFFFF
    assert bhd5.hash_path("CHR\\c2010.chrbnd.dcx", bhd5.DS3) == h
    assert bhd5.hash_path("/chr/c2010.chrbnd.dcx", bhd5.DS1) == h
    h64 = 0
    for c in "/chr/c2010.chrbnd.dcx":
        h64 = (h64 * 133 + ord(c)) & (2 ** 64 - 1)
    assert bhd5.hash_path("/chr/c2010.chrbnd.dcx", bhd5.ER) == h64


def test_the_games_keys_read_as_2048_bit_rsa_keys():
    for game, keys in bhd5_keys.ARCHIVE_KEYS.items():
        for name, text in keys.items():
            key = bhd5.parse_public_key(text)
            assert key.n.bit_length() == 2048 and key.size == 256, (game, name)
    assert bhd5_keys.key_for("Elden Ring", "data0") == bhd5_keys.ARCHIVE_KEYS["Elden Ring"]["Data0"]
    assert bhd5_keys.key_for("Elden Ring", "sd\\sd") is not None
    assert bhd5_keys.key_for("Bloodborne", "Data0") is None


def test_rsa_and_aes_round_trip(keypair):
    n, e, d, pem = keypair
    key = bhd5.parse_public_key(pem)
    assert (key.n, key.e) == (n, e)
    for fmt in bhd5.FORMATS:
        files = [("/chr/c2010.chrbnd.dcx", b"model data " * 50), ("/parts/wp_a_0100.partsbnd.dcx", b"x" * 33)]
        head, data = synth.build_bhd5(files, fmt, aes=fmt != "DS1")
        enc = bhd5.encrypt_header_for_tests(head, n, d)
        assert bhd5.is_encrypted(enc)
        header = bhd5.load_header(enc, fmt, key)
        by_hash = header.by_hash()
        import io

        for path, content in files:
            entry = by_hash[bhd5.hash_path(path, fmt)]
            assert bhd5.read_entry(io.BytesIO(data), entry) == content
    with pytest.raises(bhd5.FormatError, match="no key"):
        bhd5.load_header(enc, "DS3", None)


def test_names_are_guessed_from_the_pattern_of_known_ones():
    fmt = bhd5.DS3
    known = {bhd5.hash_path(p, fmt): p for p in ("/chr/c2010.chrbnd.dcx", "/parts/wp_a_0100_l.partsbnd.dcx")}
    wanted = {bhd5.hash_path(p, fmt) for p in ("/chr/c5310.chrbnd.dcx", "/parts/wp_a_9999_l.partsbnd.dcx",
                                                "/chr/c5310.behbnd.dcx")}
    guessed = unpack.guess_names(known, wanted, fmt)
    assert sorted(guessed.values()) == ["/chr/c5310.chrbnd.dcx", "/parts/wp_a_9999_l.partsbnd.dcx"]
    er = {bhd5.hash_path("/asset/aeg/aeg001/aeg001_000.geombnd.dcx", bhd5.ER): "/asset/aeg/aeg001/aeg001_000.geombnd.dcx"}
    want = bhd5.hash_path("/asset/aeg/aeg001/aeg001_731.geombnd.dcx", bhd5.ER)
    assert unpack.guess_names(er, {want}, bhd5.ER) == {want: "/asset/aeg/aeg001/aeg001_731.geombnd.dcx"}


def test_groups_take_models_textures_and_animations_only():
    assert unpack.group_of("/chr/c2010.chrbnd.dcx") == "characters"
    assert unpack.group_of("/chr/c2010_h.texbnd.dcx") == "characters"
    assert unpack.group_of("/chr/c2010_div00.anibnd.dcx") == "characters"
    assert unpack.group_of("/chr/c2010.behbnd.dcx") is None
    assert unpack.group_of("/parts/WP_A_0100.partsbnd.dcx") == "parts"
    assert unpack.group_of("/asset/aet/aet001/aet001_000.tpf.dcx") == "objects"
    assert unpack.group_of("/asset/aeg/aeg001/aeg001_000_l.geomhkxbnd.dcx") is None
    assert unpack.group_of("/map/m10_00_00_00/m10_00_00_00_000100.mapbnd.dcx") == "maps"
    assert unpack.group_of("/model/chr/c1000.bnd") == "characters"  # Dark Souls II
    assert unpack.group_of("/menu/menu.bnd") is None
    assert unpack.group_of("/sfx/frpg_sfxbnd_common.ffxbnd.dcx") is None


def test_the_bundled_lists_name_the_games_models():
    for spec in unpack.GAME_ARCHIVES:
        names = unpack.known_names(spec)
        assert len(names) > 1000, spec.game
    er = unpack.known_names(unpack.archives_for("Elden Ring"))
    assert bhd5.hash_path("/chr/c0000.chrbnd.dcx", bhd5.ER) in er
    wanted = [p for p in er.values() if unpack.group_of(p)]
    assert sum(1 for p in wanted if p.startswith("/asset/aeg/")) > 10000 and len(wanted) < len(er)


def test_unpack_plan_and_run(packed_ds3, tmp_path):
    game, lists = packed_ds3
    cache = tmp_path / "cache"
    found = unpack.detect(game.parent)
    assert found and found[0].game == "Dark Souls III" and found[1] == game
    dest = tmp_path / "library" / "Dark Souls III"
    plan = unpack.plan(game.parent, dest, None, cache, lists)
    summary = plan.summary()
    by_group = {g["id"]: g for g in summary["groups"]}
    assert by_group["parts"]["files"] == 4  # wp_a_0300 (+_l guessed), wp_a_0301 guessed, hd_m_1000
    assert by_group["characters"]["files"] == 2  # chrbnd + texbnd; the behbnd isn't wanted
    assert summary["guessed"] == 2 and summary["unknown"] == 1  # wp_a_7777 doesn't hold wp_a_7777
    assert {a["name"] for a in summary["archives"]} == {"Data1", "Data2"}
    assert list((cache / "Dark Souls III").glob("Data1-*.bhd5"))  # kept decrypted for next time

    result = unpack.run(plan, ["parts"])
    assert result["written"] == 4 and not result["failed"]
    assert result["oodle"] == ["oo2core_6_win64.dll"]
    weapon = dest / "parts" / "wp_a_0300.partsbnd.dcx"
    original = (tmp_path / "DARK SOULS III" / "Game" / "parts" / "wp_a_0300.partsbnd.dcx").read_bytes()
    assert weapon.read_bytes() == original  # AES undone, padding cut
    assert not (dest / "chr").exists()
    assert unpack.read_note(dest)["groups"] == ["parts"]
    # again: what's there already is left alone, the rest comes
    again = unpack.plan(game.parent, dest, None, cache, lists)
    assert {g["id"]: g["to_unpack"] for g in again.summary()["groups"]}["parts"] == 0
    result = unpack.run(again, ["parts", "characters"])
    assert result["written"] == 2 and result["present"] == 4
    assert (dest / "chr" / "c1100.texbnd.dcx").is_file()
    assert not (dest / "chr" / "c1100.behbnd.dcx").exists()


def test_dark_souls_2_keys_come_from_its_pem_files(tmp_path, keypair):
    files = [("/model/chr/c1000.bnd", b"chr"), ("/model/parts/weapon/wp_1000_m.bnd", b"wp")]
    game = tmp_path / "Dark Souls II Scholar of the First Sin" / "Game"
    pack_game(game, files, {"GameDataEbl": ["/"]}, "DS2", keypair)
    found = unpack.detect(game.parent)
    assert found is None  # no key file yet
    (game / "GameDataKeyCode.pem").write_text(keypair[3])
    spec, _where = unpack.detect(game.parent)
    assert spec.game == "Dark Souls II"
    lists = tmp_path / "lists"
    lists.mkdir()
    (lists / "DarkSouls2Dictionary.txt").write_text("\n".join(p for p, _ in files))
    plan = unpack.plan(game.parent, tmp_path / "out", None, None, lists)
    assert sorted(f.path for f in plan.files) == sorted(p for p, _ in files)


def test_dark_souls_ptde_headers_are_not_encrypted(tmp_path):
    files = [("/chr/c2240.chrbnd.dcx", b"chr"), ("/obj/o1000.objbnd.dcx", b"obj")]
    game = tmp_path / "DARK SOULS Prepare To Die Edition" / "DATA"
    pack_game(game, files, {"dvdbnd0": ["/"]}, "DS1", None, ext=".bhd5", aes=False)
    spec, _where = unpack.detect(game)
    assert spec.game == "Dark Souls"
    plan = unpack.plan(game, tmp_path / "out", None, None, None)
    unpack.run(plan, ["characters", "objects"])
    assert (tmp_path / "out" / "chr" / "c2240.chrbnd.dcx").read_bytes() == b"chr"


def test_downloaded_lists_are_checked_and_kept(tmp_path):
    asked = []

    def fake(url):
        asked.append(url)
        if "Scholar" in url:
            raise OSError("offline")
        return ("#data0\n" + "\n".join(f"/chr/c{i:04d}.chrbnd.dcx" for i in range(200))).encode()

    out = unpack.download_lists(tmp_path / "lists", ["Dark Souls II"], opener=fake)
    assert out["downloaded"] == ["DarkSouls2Dictionary.txt"] and len(out["failed"]) == 1
    assert all(u.startswith("https://raw.githubusercontent.com/Nordgaren/") for u in asked)
    status = {s["game"]: s for s in unpack.lists_status(tmp_path / "lists")}
    assert status["Dark Souls II"]["downloaded"] == ["DarkSouls2Dictionary.txt"]
    assert status["Elden Ring"]["bundled"] == ["eldenring.txt.gz"] and not status["Elden Ring"]["downloaded"]
    assert unpack.download_lists(tmp_path / "l2", ["Sekiro"], opener=lambda u: b"<html>not found</html>")["failed"]


def test_unpacking_through_the_engine(packed_ds3, tmp_path, monkeypatch):
    game, lists = packed_ds3
    monkeypatch.setattr(unpack, "bundled_lists", lambda spec: [])
    svc = Service(Config(library_dir=str(tmp_path / "library")), tmp_path / "cache")
    monkeypatch.setattr("darkstl.app.service.filelists_dir", lambda: lists)
    added = wait(svc, svc.cmd_folder_add({"path": str(game.parent)})["job"])
    assert added["game"] == "Dark Souls III" and added["count"] == 0
    overview = svc.cmd_overview({})
    folder = overview["games"][0]["folders"][0]
    assert folder["packed"] == {"game": "Dark Souls III"}
    plan = wait(svc, svc.cmd_unpack_plan({"path": str(game.parent)})["job"])
    assert plan["dest"] == str(tmp_path / "library" / "Dark Souls III")
    assert {g["id"]: g["files"] for g in plan["groups"]}["parts"] == 4
    with pytest.raises(AppError):
        svc.cmd_unpack_run({"path": str(game.parent), "groups": []})
    done = wait(svc, svc.cmd_unpack_run({"path": str(game.parent), "groups": ["parts", "characters"]})["job"])
    assert done["written"] == 6 and done["count"] >= 4
    overview = svc.cmd_overview({})
    stems = {m["stem"].lower() for m in svc.cmd_models_query({"game": "Dark Souls III"})["items"]}
    assert {"wp_a_0300", "wp_a_0301", "hd_m_1000", "c1100"} <= stems
    folders = {f["path"]: f for f in overview["games"][0]["folders"]}
    assert folders[done["dest"]]["collected"]
    assert svc.cmd_filelists({})["folder"] == str(lists)


def test_unpack_from_the_command_line(packed_ds3, tmp_path, monkeypatch, capsys):
    from darkstl import cli

    game, lists = packed_ds3
    monkeypatch.setenv("DARKSTL_HOME", str(tmp_path / "home"))
    monkeypatch.setattr("darkstl.app.config.filelists_dir", lambda: lists)
    out = tmp_path / "out"
    assert cli.main(["unpack", str(game.parent), "-o", str(out), "--dry-run"]) == 0
    assert "parts" in capsys.readouterr().out and not out.exists()
    assert cli.main(["unpack", str(game.parent), "-o", str(out), "--only", "parts"]) == 0
    assert "Wrote 4 files" in capsys.readouterr().out
    assert (out / "parts" / "wp_a_0301.partsbnd.dcx").is_file() and not (out / "chr").exists()
    assert cli.main(["unpack", str(game.parent), "--only", "weapons"]) == 2
