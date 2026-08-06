#!/usr/bin/env python3
"""
熊本地震 道路情報 市町村巡回クローラー（GitHub Actions版）

- sources.json の各URL（全URL実fetchで存在確認済み）を巡回
- 本文ハッシュが前回と変化したページのみ claude -p で規制情報を抽出
  （認証: ローカルはclaudeログイン済み資格情報、CIは環境変数 CLAUDE_CODE_OAUTH_TOKEN）
- 結果を municipal.json にマージ（コミット・デプロイはワークフロー側が実施。本スクリプトは書くだけ）

安全フィルタ（劣化させないこと。テスト: test_crawl_ci.py）
 1. 地震（2026-07-28）より前の記事・日付不明の記事は破棄
 2. 引用・路線名が原文に実在しない項目は破棄（AI転記ミス・捏造防止）
 3. 巡回対象外になった市町村の残存項目を掃除

サーバー負荷配慮: 1サイト1リクエスト・タイムアウト15秒・サイト間1秒待機。
"""
import hashlib
import html as htmlmod
import json
import os
import re
import signal
import subprocess
import sys
import time
import unicodedata
import urllib.request
from datetime import datetime, timezone, timedelta

BASE = os.path.dirname(os.path.abspath(__file__))
SOURCES_PATH = os.path.join(BASE, "sources.json")
STATE_PATH = os.path.join(BASE, "state", "hashes.json")
MUNICIPAL_PATH = os.path.join(BASE, "municipal.json")
TIMEOUT = 15
UA = "Mozilla/5.0 (compatible; disaster-info-aggregator)"
JST = timezone(timedelta(hours=9))

MAX_TEXT_FOR_CLAUDE = 9000  # 抽出に渡す本文の上限（文字）
QUAKE_DATE = "2026-07-28"   # 令和8年熊本地震の発生日。これより前の記事は採用しない

EXTRACT_PROMPT = """以下は熊本県{municipality}の公式サイト「{page_title}」({url})の本文テキストです。
このページに明記された「道路の通行規制・通行止め・復旧情報」のみをJSON配列で抽出してください。

ルール（厳守）:
- ページに明記された情報のみ。推測・補完・一般知識による追加は禁止。
- **2026年7月28日の地震（令和8年熊本地震）以降に発表・更新された情報のみを対象とする。**
  記事日付・更新日付が地震より前のもの（令和7年以前、2025年以前、平成・令和2年の豪雨関連等）は除外。
- 各項目に、その記事・お知らせの発表日または更新日を articleDate（YYYY-MM-DD形式）として付ける。
  和暦は西暦へ変換（令和8年=2026年）。日付が本文から特定できない場合は "" とする。
- 道路の通行規制・復旧の記載が無ければ空配列 [] だけを出力。
- 避難所・給水・ごみ・施設休館など道路以外の情報は含めない。
- 各項目に原文の該当箇所を80字以内で短く引用する。
- 出力はJSON配列のみ。説明文・コードブロック記法は書かない。

出力形式:
[{{"road": "路線名や場所（原文の表記のまま）", "type": "国道/県道/市道/町道/村道/農道/林道/その他 のいずれか（原文から判断できなければ その他）", "status": "全面通行止め/片側交互通行止め/通行止め解除/その他規制 など原文に基づく状態", "articleDate": "YYYY-MM-DD（発表日・更新日。不明なら空文字）", "sourceQuote": "原文の該当箇所の短い引用（80字以内）"}}]

--- 本文テキスト ---
{body}
"""


def jst_now():
    return datetime.now(JST).strftime("%Y-%m-%d %H:%M")


def log(msg):
    print("[%s] %s" % (jst_now(), msg), flush=True)


def fetch_page(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as res:
        raw = res.read()
    for enc in ("utf-8", "cp932", "euc-jp"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


def html_to_text(page):
    page = re.sub(r"(?is)<(script|style|nav|header|footer)[^>]*>.*?</\1>", " ", page)
    page = re.sub(r"(?is)<!--.*?-->", " ", page)
    text = re.sub(r"<[^>]+>", " ", page)
    text = htmlmod.unescape(text)
    text = re.sub(r"[ \t　]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
        f.write("\n")
    os.replace(tmp, path)


def _norm(s):
    """原文照合用の正規化（空白除去＋NFKC。康熙部首異体字などを吸収）"""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", s))


def validate_items(items, municipality, url, body_text):
    """抽出結果の安全フィルタ（純関数。テスト対象）
    - 原文引用なし → 破棄
    - articleDate が不明・不正・地震(QUAKE_DATE)より前 → 破棄
    - 引用または路線名が原文に実在しない → 破棄（照合不一致件数を返す）
    戻り値: (採用項目リスト, 照合不一致で破棄した件数)"""
    norm_body = _norm(body_text)
    mismatched = 0
    cleaned = []
    for it in items:
        if not isinstance(it, dict):
            continue
        road = str(it.get("road", "")).strip()
        quote = str(it.get("sourceQuote", "")).strip()
        if not road or not quote:
            continue  # 原文引用の無い項目は採用しない（捏造防止）
        article_date = str(it.get("articleDate", "")).strip()
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", article_date):
            log("破棄(日付不明): %s %s" % (municipality, road))
            continue
        if article_date < QUAKE_DATE:
            log("破棄(地震前の記事 %s): %s %s" % (article_date, municipality, road))
            continue
        if _norm(quote) not in norm_body or _norm(road) not in norm_body:
            log("破棄(原文照合不一致): %s %s" % (municipality, road))
            mismatched += 1
            continue
        cleaned.append({
            "municipality": municipality,
            "road": road,
            "type": str(it.get("type", "その他")).strip() or "その他",
            "status": str(it.get("status", "")).strip() or "規制情報",
            "articleDate": article_date,
            "sourceUrl": url,
            "sourceQuote": quote[:120],
            "fetchedAt": jst_now(),
        })
    return cleaned, mismatched


def drop_orphans(municipal, ok_names):
    """巡回対象外になった市町村の残存項目を掃除（純関数。テスト対象）。削除件数を返す"""
    items = municipal.get("items", [])
    kept = [it for it in items if it.get("municipality") in ok_names]
    dropped = len(items) - len(kept)
    municipal["items"] = kept
    return dropped


def extract_with_claude(municipality, page_title, url, body_text):
    """変化のあったページのみ claude -p で規制情報を抽出。
    認証: ローカル=claudeログイン資格情報 / CI=CLAUDE_CODE_OAUTH_TOKEN（環境変数のまま渡す）
    戻り値: (採用項目リスト, 照合不一致で破棄した件数) / 失敗時は (None, 0)"""
    prompt = EXTRACT_PROMPT.format(
        municipality=municipality, page_title=page_title, url=url,
        body=body_text[:MAX_TEXT_FOR_CLAUDE],
    )
    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)  # 従量APIキーを確実に使わせない（サブスク認証のみ）
    # claudeは内部で子プロセスを持つため、タイムアウト時は
    # プロセスグループごとSIGKILLしないとパイプが開いたまま永久に待つ
    try:
        proc = subprocess.Popen(
            ["claude", "-p", prompt, "--model", "haiku", "--output-format", "text"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
            start_new_session=True,
        )
    except FileNotFoundError:
        log("ERROR: claude CLIが見つかりません")
        return None, 0
    try:
        stdout, stderr = proc.communicate(timeout=180)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            proc.communicate(timeout=10)
        except Exception:
            pass
        log("ERROR: claude -p タイムアウト・グループ強制終了 (%s)" % municipality)
        return None, 0
    if proc.returncode != 0:
        log("ERROR: claude -p 失敗 (%s): %s" % (municipality, stderr.strip()[:150]))
        return None, 0
    out = stdout.strip()
    m = re.search(r"\[.*\]", out, re.S)
    if not m:
        log("WARN: 抽出結果にJSON配列なし (%s): %s" % (municipality, out[:100]))
        return None, 0
    try:
        items = json.loads(m.group(0))
    except json.JSONDecodeError:
        log("WARN: 抽出結果のJSONが不正 (%s)" % municipality)
        return None, 0
    if not isinstance(items, list):
        return None, 0
    return validate_items(items, municipality, url, body_text)


def main():
    force = "--force" in sys.argv  # ハッシュ無視で全ページ抽出

    conf = load_json(SOURCES_PATH, None)
    if not conf:
        log("ERROR: sources.json を読めません")
        sys.exit(1)

    state = load_json(STATE_PATH, {})
    municipal = load_json(MUNICIPAL_PATH, {"updatedAt": None, "items": []})

    ok_sources = [s for s in conf.get("sources", []) if s.get("status") == "ok"]
    log("巡回開始: %d サイト%s" % (len(ok_sources), "（--force）" if force else ""))

    before_items = json.dumps(municipal.get("items", []), ensure_ascii=False, sort_keys=True)

    # 安全フィルタ3: 巡回対象外の市町村の残存項目を掃除
    dropped = drop_orphans(municipal, set(s["municipality"] for s in ok_sources))
    if dropped:
        log("巡回対象外の市町村の残存項目を削除: %d件" % dropped)

    changed_count = 0
    deadline = time.monotonic() + 660  # 全体予算11分。超えた残りは次回巡回に回す（Actionsの15分制限に届かせない）
    for src in ok_sources:
        if time.monotonic() > deadline:
            log("WARN: 全体予算超過のため残りサイトを次回に回す（%s以降）" % src["municipality"])
            break
        name = src["municipality"]
        url = src["url"]
        try:
            page = fetch_page(url)
        except Exception as e:
            log("WARN: 取得失敗 %s (%s): %s" % (name, url, str(e)[:80]))
            time.sleep(1)
            continue
        text = html_to_text(page)
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        prev = state.get(url, {}).get("hash")
        if digest == prev and not force:
            log("変化なし: %s" % name)
            time.sleep(1)
            continue

        log("変化検知: %s → claude -p で抽出" % name)
        items, mismatched = extract_with_claude(name, src.get("pageTitle", ""), url, text)
        if items is None:
            # 抽出失敗時はハッシュを更新しない（次回再試行）
            time.sleep(1)
            continue
        municipal["items"] = [it for it in municipal.get("items", [])
                              if it.get("municipality") != name] + items
        if mismatched == 0:
            state[url] = {"hash": digest, "checkedAt": jst_now()}
        else:
            log("照合不一致%d件のためハッシュ未更新（次回再抽出）: %s" % (mismatched, name))
        changed_count += 1
        log("抽出完了: %s %d件" % (name, len(items)))
        # 進捗を失わないよう1サイトごとに保存
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        save_json(STATE_PATH, state)
        save_json(MUNICIPAL_PATH, municipal)
        time.sleep(1)

    after_items = json.dumps(municipal.get("items", []), ensure_ascii=False, sort_keys=True)
    data_changed = before_items != after_items

    if data_changed:
        municipal["updatedAt"] = jst_now()
        municipal["note"] = "市町村公式サイトからの自動収集データ。各項目に原文リンク・引用・取得時刻を付与。"
        save_json(MUNICIPAL_PATH, municipal)
        log("municipal.json 更新: 全%d件" % len(municipal["items"]))
    else:
        log("municipal.json 変更なし")
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    save_json(STATE_PATH, state)
    log("巡回終了（コミット・デプロイはワークフロー側の担当）")


if __name__ == "__main__":
    main()
