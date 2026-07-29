#!/usr/bin/env python3
"""安全フィルタ3点の回帰テスト（python3 test_crawl_ci.py で実行）"""
import unittest

import crawl_ci


BODY = "令和8年7月29日更新 町道中央1号線（路面陥没により全面通行止め） そのほかのお知らせ"


def item(**kw):
    base = {
        "road": "町道中央1号線",
        "type": "町道",
        "status": "全面通行止め",
        "articleDate": "2026-07-29",
        "sourceQuote": "町道中央1号線（路面陥没により全面通行止め）",
    }
    base.update(kw)
    return base


class TestValidateItems(unittest.TestCase):
    def _run(self, items, body=BODY):
        return crawl_ci.validate_items(items, "テスト町", "https://example.jp/", body)

    def test_valid_item_passes(self):
        cleaned, mismatched = self._run([item()])
        self.assertEqual(len(cleaned), 1)
        self.assertEqual(mismatched, 0)
        self.assertEqual(cleaned[0]["municipality"], "テスト町")
        self.assertEqual(cleaned[0]["articleDate"], "2026-07-29")

    # フィルタ1: 地震前日付・日付不明の破棄
    def test_pre_quake_date_discarded(self):
        cleaned, _ = self._run([item(articleDate="2025-06-30")])
        self.assertEqual(cleaned, [])

    def test_quake_day_kept(self):
        cleaned, _ = self._run([item(articleDate="2026-07-28")])
        self.assertEqual(len(cleaned), 1)

    def test_unknown_date_discarded(self):
        for bad in ("", "不明", "令和8年7月29日"):
            cleaned, _ = self._run([item(articleDate=bad)])
            self.assertEqual(cleaned, [], "articleDate=%r が破棄されていない" % bad)

    # フィルタ2: 原文照合（引用・路線名の実在確認）
    def test_quote_not_in_body_discarded(self):
        cleaned, mismatched = self._run([item(sourceQuote="存在しない引用文")])
        self.assertEqual(cleaned, [])
        self.assertEqual(mismatched, 1)

    def test_road_not_in_body_discarded(self):
        cleaned, mismatched = self._run([item(road="存在しない橋")])
        self.assertEqual(cleaned, [])
        self.assertEqual(mismatched, 1)

    def test_glyph_variant_matches_via_nfkc(self):
        # 康熙部首異体字（⾏⽌など）を含む原文でも照合が通ること
        body = "令和8年7月29日更新 町道中央1号線（路面陥没により全面通⾏⽌め）"
        cleaned, mismatched = crawl_ci.validate_items(
            [item(sourceQuote="町道中央1号線（路面陥没により全面通行止め）")],
            "テスト町", "https://example.jp/", body)
        self.assertEqual(len(cleaned), 1)
        self.assertEqual(mismatched, 0)

    def test_missing_quote_discarded(self):
        cleaned, _ = self._run([item(sourceQuote="")])
        self.assertEqual(cleaned, [])

    # フィルタ3: 巡回対象外の市町村の掃除
    def test_drop_orphans(self):
        municipal = {"items": [
            {"municipality": "対象町", "road": "a"},
            {"municipality": "除外村", "road": "b"},
        ]}
        dropped = crawl_ci.drop_orphans(municipal, {"対象町"})
        self.assertEqual(dropped, 1)
        self.assertEqual([i["municipality"] for i in municipal["items"]], ["対象町"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
