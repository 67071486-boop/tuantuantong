"""回调事件文件的分段**保留期清理**（业务方 2026-10-26 拍板：保留 24 小时，处理完的旧分段删掉）。

判据形态（照 `test_callback_receiver.JsonlRotationTests` 的同一套）：
`ROTATE_BYTES` 压小 ⇒ 真轮转出归档段 ⇒ 再按「已消费偏移 + 段龄」决定删不删。

⭐ 这一批的**承重**不是「删掉了」而是「删段不让消费侧误判文件被搬动」：
`snapshot().high_water_bytes`（累计写入字节）在删段前后**必须不变**，
而 `total_bytes`（现存字节）**必须变小** —— 后者是「这条用例真的删到了东西」的自证。
"""
from __future__ import annotations

import os
import shutil
import tempfile
import time
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase

from apps.callback import store


class _StoreCase(SimpleTestCase):
    """一个临时事件文件（⛔ 不碰仓库里真的 `shared/callback/`）。"""

    ROW_PADDING = "。" * 40

    def setUp(self) -> None:
        super().setUp()
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.events = self.tmp / "shared" / "callback" / "events.jsonl"

    def _row(self, seq: int) -> dict:
        return {
            "record_version": 4,
            "seq": seq,
            "body_text": f"seq={seq} {self.ROW_PADDING}",
        }

    def _write(self, count: int, *, start: int = 0, rotate_at: int = 400) -> None:
        """写 `count` 行；`rotate_at` 压小 ⇒ 真的轮转出归档段。"""
        with mock.patch.object(store, "ROTATE_BYTES", rotate_at):
            for seq in range(start, start + count):
                store.append_record(self._row(seq), path=self.events)

    def _segments(self) -> list[tuple[int, Path]]:
        return store.segment_files(self.events)

    def _age(self, entry: Path, seconds: int) -> None:
        """把某一段的 mtime 改成 `现在 - seconds`（段的 mtime = 它最后一笔写入的时刻）。"""
        stamp = time.time() - seconds
        os.utime(entry, (stamp, stamp))

    def _assert_rotated(self, *, at_least: int = 2) -> list[tuple[int, Path]]:
        segments = self._segments()
        self.assertGreaterEqual(
            len(segments), at_least, "自证失败：这一份夹具没有真的轮转出归档段",
        )
        for (base, entry), (next_base, _next) in zip(segments, segments[1:]):
            self.assertEqual(base + entry.stat().st_size, next_base, "段与段必须首尾相接")
        return segments


class SegmentRetentionTests(_StoreCase):
    def test_a_fully_consumed_segment_past_the_retention_window_is_deleted(self):
        """① 已完全消费 + 超过保留期 ⇒ 删掉（且只删这一段）。"""
        self._write(12)
        segments = self._assert_rotated()
        oldest, second = segments[0][1], segments[1][1]
        #: 已完全消费 = 消费侧偏移已越过这一段的段尾（= 下一段起始字节）。
        consumed = segments[1][0]
        self._age(oldest, store.SEGMENT_RETENTION_SECONDS + 60)

        deleted = store.prune_segments(consumed, path=self.events)

        self.assertEqual(deleted, [oldest], "应当只删那一段超期且已消费的归档段")
        self.assertFalse(oldest.exists(), "超期 + 已消费的归档段没有被删掉")
        self.assertTrue(second.exists(), "没消费完的那一段被误删了")

    def test_a_fully_consumed_segment_inside_the_retention_window_is_kept(self):
        """② 已完全消费、但还没到 24 小时 ⇒ 一个字节都不删。"""
        self._write(12)
        segments = self._assert_rotated()
        oldest = segments[0][1]
        self._age(oldest, store.SEGMENT_RETENTION_SECONDS - 3600)

        deleted = store.prune_segments(segments[1][0], path=self.events)

        self.assertEqual(deleted, [])
        self.assertTrue(oldest.exists(), "还没到保留期的段被删掉了")

    def test_a_segment_the_consumer_has_not_finished_is_never_deleted(self):
        """③ 消费侧偏移还落在段内 ⇒ 这一段**绝不删**（哪怕它早就超期了）。"""
        self._write(12)
        segments = self._assert_rotated()
        for _base, entry in segments:
            self._age(entry, store.SEGMENT_RETENTION_SECONDS + 86400)
        #: 偏移落在第一段内部（没越过它的段尾）。
        inside = segments[0][0] + 1

        deleted = store.prune_segments(inside, path=self.events)

        self.assertEqual(deleted, [], "没消费完的段被删了 ⇒ 消费侧会丢行")
        for _base, entry in segments:
            self.assertTrue(entry.exists())

    def test_deleting_a_segment_keeps_the_high_water_bytes(self):
        """④ 承重：删段让「现存字节」变小，但**高水位不变**（消费侧的判据必须用它）。"""
        self._write(12)
        segments = self._assert_rotated()
        before = store.snapshot(self.events)
        self.assertEqual(before.high_water_bytes, before.total_bytes, "没有删段时两者必须相等")
        self._age(segments[0][1], store.SEGMENT_RETENTION_SECONDS + 60)

        self.assertEqual(store.prune_segments(segments[1][0], path=self.events), [segments[0][1]])

        after = store.snapshot(self.events)
        self.assertEqual(
            after.high_water_bytes, before.high_water_bytes,
            "删段把高水位弄小了 ⇒ 消费侧会把清理误判成「文件被搬动」并水位清零",
        )
        self.assertLess(
            after.total_bytes, before.total_bytes,
            "自证失败：现存字节没变小（说明这一段其实没删掉）",
        )
        self.assertEqual(after.earliest_base, segments[1][0], "现存最早段应当前移")

    def test_reading_after_a_prune_sees_only_the_new_rows(self):
        """⑤ 删段后继续从当前偏移读：新行照常读到、旧行不重复（不丢行）。"""
        self._write(12)
        segments = self._assert_rotated()
        whole = store.read_records_from(self.events, 0)
        offset = whole.new_offset
        self.assertEqual([row["seq"] for row in whole.records], list(range(12)))
        self._age(segments[0][1], store.SEGMENT_RETENTION_SECONDS + 60)
        self.assertEqual(store.prune_segments(offset, path=self.events), [segments[0][1]])

        self._write(3, start=12)

        tail = store.read_records_from(self.events, offset)
        self.assertEqual(
            [row["seq"] for row in tail.records], [12, 13, 14],
            "删段之后新行必须照常读到、且旧行不许重复出现",
        )
        self.assertEqual(tail.new_offset, store.snapshot(self.events).high_water_bytes)

    def test_prune_without_any_archive_segment_is_a_no_op(self):
        """⑥ 只有一个现役文件（还没轮转过）⇒ 清空是 no-op（⛔ 不许动现役文件）。"""
        self._write(2, rotate_at=store.ROTATE_BYTES)
        self.assertEqual(self._segments(), [], "自证失败：这一份夹具不该有归档段")
        before = store.snapshot(self.events)

        self.assertEqual(store.prune_segments(before.high_water_bytes, path=self.events), [])

        self.assertTrue(self.events.exists(), "现役文件被清理动过了")
        self.assertEqual(store.snapshot(self.events), before)
        self.assertEqual(
            len(list(store.iter_lines(self.events))), 2, "现役文件里的行少掉了",
        )

    def test_prune_on_a_missing_filesystem_is_a_no_op(self):
        """目录都还不存在（接收器一次都没写过）⇒ no-op，⛔ 不许为了清理去建目录。"""
        self.assertEqual(store.prune_segments(0, path=self.events), [])
        self.assertFalse(self.events.parent.exists(), "清理顺手把事件目录建出来了")

    def test_an_offset_older_than_the_earliest_segment_is_a_loud_error(self):
        """③（响应项）起点早于现存最早段 ⇒ **响亮报错**，⛔ 不静默从头读。"""
        self._write(12)
        segments = self._assert_rotated()
        self._age(segments[0][1], store.SEGMENT_RETENTION_SECONDS + 60)
        self.assertEqual(store.prune_segments(segments[1][0], path=self.events), [segments[0][1]])
        earliest = segments[1][0]
        self.assertGreater(earliest, 0, "自证失败：现存最早段还是 0")

        with self.assertRaises(store.OffsetOutOfRetention) as caught:
            store.read_records_from(self.events, earliest - 1)

        message = str(caught.exception)
        self.assertIn(str(earliest - 1), message, "报错里要带上那个偏移（排查要能一眼看到）")
        self.assertIn(str(earliest), message, "报错里要带现存最早段的段首")

    def test_reading_the_retained_stream_from_its_earliest_base_still_works(self):
        """上一条的对照：从 `snapshot().earliest_base` 起读**必须读得到**（水位清零那一支用它）。"""
        self._write(12)
        segments = self._assert_rotated()
        self._age(segments[0][1], store.SEGMENT_RETENTION_SECONDS + 60)
        store.prune_segments(segments[1][0], path=self.events)
        snap = store.snapshot(self.events)

        chunk = store.read_records_from(self.events, snap.earliest_base)

        self.assertTrue(chunk.records, "从现存最早段读起却一条都读不到")
        self.assertEqual(
            chunk.new_offset, snap.high_water_bytes, "从现存最早段读到底应当正好追平高水位",
        )


class SegmentRetentionConfigTests(SimpleTestCase):
    """保留期可 env 覆盖（排查 / 演练时临时缩短），非法值回落到常量并响亮警告。"""

    ENV = store.ENV_SEGMENT_RETENTION

    def _retention(self) -> int:
        return store.segment_retention_seconds()

    def test_the_default_is_24_hours(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(self.ENV, None)
            self.assertEqual(self._retention(), 24 * 3600)
            self.assertEqual(store.SEGMENT_RETENTION_SECONDS, 24 * 3600)

    def test_the_env_override_is_honored(self):
        with mock.patch.dict(os.environ, {self.ENV: "60"}):
            self.assertEqual(self._retention(), 60)

    def test_invalid_values_fall_back_to_the_constant_loudly(self):
        for raw in ("", "0", "-1", "abc", "1.5"):
            with self.subTest(raw=raw):
                with mock.patch.dict(os.environ, {self.ENV: raw}):
                    with self.assertLogs("apps.callback.store", level="WARNING"):
                        self.assertEqual(self._retention(), store.SEGMENT_RETENTION_SECONDS)
