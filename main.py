#!/usr/bin/env python3
"""
main.py
「順番指定予約」の待ち人数を観測し、指定した着席目標時刻に合わせて「今申し込むべき」タイミングを通知する。

モデル設計:
- 待ち時間の変換は Little's law 的な近似: テーブル30卓(カウンター14席は
  スコープ外)が並列にお客を捌くサーバーだと見て、N組待ちなら
  wait_minutes(N) = N * τ / テーブル数 (τ=1卓あたり平均滞在時間)。
  τは実測(申込み時のN ↔ 実際に呼ばれた時間)が無いと原理的に決まらない
  (列に並ばずに到着率と捌き率を分離することはできない)。
  そのため実行中に `calibrate N 実測待ち分` で即座に反映できるようにしている。
- 傾き推定は複数スケールの窓で m/w (w分でm人増えた、のペース)を計算し、
  短い窓と一段広い窓の符号が一致する最短の窓を採用する。
  不一致(=山の頂点付近などの転換点)なら自動でより広い窓にフォールバックする。
- 表示を更新するには画面上の「更新」ボタンをタップする必要があるため、
  毎回のOCR前に CFG.tap_pos を adb でタップしてから撮影する
  (tap_posが未設定ならタップせず撮影のみ)。
  タップ位置・OCR切り出し範囲(crop_box)は configure_regions.py で
  視覚的に指定して calib.json に保存したものを起動時に読み込む。
- scrcpy はユーザーが画面を見て実際にタップするための窓として開いたままでよい。
  この監視処理自体は adb に直接アクセスするので scrcpy を経由する必要はない。
- 標準入力をバックグラウンドスレッドで監視し、再起動なしにパラメータを
  変更できる(フルTUIではなくコマンド入力。`help`で一覧表示)。

使い方:
  python3 configure_regions.py          # 先にタップ位置/切り出し範囲を視覚的に指定
  python3 main.py --target-time 19:00                        # 実機監視
  python3 main.py --target-time 19:00 --avg-stay-min 32 --tables 30  # 初期値指定
  python3 main.py --target-time 19:00 --simulate             # ロジック確認
  (監視中の入力例)
    calibrate 20 30        # N=20で申込んで実際30分待った → τを逆算してセット
    avg_stay_min 32        # 平均滞在時間を直接指定
    target 19:30           # 目標着席時刻を変更
    tol 3                  # 許容誤差を変更
    tap 540 1820           # 更新ボタンの座標を直接指定
    crop 300 800 700 880   # OCR切り出し範囲を直接指定
    poll_interval 30       # ポーリング間隔を変更(秒)
    show / help
"""

import argparse
import json
import math
import re
import statistics
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional, Tuple

import pytesseract

from adb_device import adb_screencap, adb_tap

pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

LOOKAHEAD_MIN: float = 1.0  # 通知後、実際にタップするまでの反応時間バッファ(分)
SLOPE_WINDOWS_MIN: Tuple[float, ...] = (
    10,
    20,
    40,
    80,
)  # 傾き推定に使う複数スケールの窓(分)
TAP_WAIT_SEC = 2  # タップ後、表示が更新されるまでの待機(秒)
CALIB_FILE = Path(__file__).resolve().parent / "calib.json"


@dataclass
class Config:
    """実行中に書き換え可能なパラメータ群。コマンド入力スレッドがここを更新する。"""

    tables: float = 30.0
    avg_stay_min: float = 35.0
    tol: timedelta = timedelta(minutes=5)
    target: datetime = field(default_factory=datetime.now)
    tap_pos: Optional[Tuple[int, int]] = None  # 更新ボタンの座標(端末ピクセル)
    crop_box: Tuple[int, int, int, int] = (0, 0, 100, 50)  # 待ち人数の表示領域(OCR用)
    poll_interval: int = 60  # ポーリング間隔(秒)


CFG = Config()


@dataclass
class Sample:
    t: datetime
    n: float


@dataclass
class Estimate:
    """1回分の評価結果。デバッグ出力・判定・次回間隔決定をすべてこれ1つから行う。"""

    t: datetime
    n_now: float
    window_used: Optional[float]
    slope: float
    n_future: float
    wait_min: float
    projected: datetime


def n_to_wait_minutes(n: float) -> float:
    """N組待ち → 推定待ち時間(分)。Little's law的近似: N*τ/テーブル数。"""
    return max(n, 0) * CFG.avg_stay_min / CFG.tables


def tap_refresh() -> None:
    """更新ボタンをタップして表示を最新化する(tap_pos未設定ならスキップ)。"""
    if CFG.tap_pos is None:
        return
    adb_tap(*CFG.tap_pos)
    time.sleep(TAP_WAIT_SEC)


def read_queue_count() -> float:
    """更新ボタンをタップ→指定領域を3回OCRしてmedianでノイズを抑える。"""
    tap_refresh()
    reads = []
    for _ in range(3):
        img = adb_screencap().crop(CFG.crop_box)
        text = pytesseract.image_to_string(img, lang="jpn", config="--psm 7")
        digits = re.sub(r"\D", "", text)
        if digits:
            reads.append(float(digits))
    if not reads:
        return float("nan")
    return statistics.median(reads)


def _interpolate_n(history: List[Sample], t: datetime) -> Optional[float]:
    """tに最も近い前後のサンプルから線形補間してNの値を返す。"""
    if not history:
        return None
    if t <= history[0].t:
        return history[0].n
    if t >= history[-1].t:
        return history[-1].n
    for i in range(1, len(history)):
        if history[i].t >= t:
            t0, n0 = history[i - 1].t, history[i - 1].n
            t1, n1 = history[i].t, history[i].n
            if t1 == t0:
                return n1
            ratio = (t - t0).total_seconds() / (t1 - t0).total_seconds()
            return n0 + (n1 - n0) * ratio
    return history[-1].n


def window_slope(history: List[Sample], window_min: float) -> Optional[float]:
    """
    直近 window_min 分間での変化量(人) / window_min = ペース(人/分)。
    窓が観測範囲を超えればNone
    """
    if len(history) < 2:
        return None
    now = history[-1].t
    past_t = now - timedelta(minutes=window_min)
    if past_t < history[0].t:
        return None
    n_now = history[-1].n
    n_past = _interpolate_n(history, past_t)
    if n_past is None:
        return None
    return (n_now - n_past) / window_min


def adaptive_slope(
    history: List[Sample], windows: Tuple[float, ...] = SLOPE_WINDOWS_MIN
) -> Tuple[Optional[float], float]:
    """戻り値: (採用した窓の長さ(分) or None, 傾き(人/分))。
    短い窓から見て、符号が一段広い窓と一致したらその窓で確定。
    傾きがゼロの場合はトレンドなしと見なし、最短の有効窓を採用する。
    符号の不一致が続けば最終的に最長窓(最も滑らかな推定)を採用する。"""
    slopes = [(w, window_slope(history, w)) for w in windows]
    slopes = [(w, s) for w, s in slopes if s is not None]
    if not slopes:
        return None, 0.0
    for (w_s, s_s), (_, s_l) in zip(slopes, slopes[1:]):
        if s_s == 0 or (s_s > 0) == (s_l > 0):
            return w_s, s_s
    return slopes[-1]


def evaluate(history: List[Sample], lookahead_min: float) -> Optional[Estimate]:
    """現在のNと傾きから、『今からlookahead_min分後に申込んだ場合』の着席予想時刻までを一括計算。"""
    if not history:
        return None
    latest = history[-1]
    window_used, slope = adaptive_slope(history)
    n_future = max(latest.n + slope * lookahead_min, 0)
    decide_time = latest.t + timedelta(minutes=lookahead_min)
    wait_min = n_to_wait_minutes(n_future)
    projected = decide_time + timedelta(minutes=wait_min)
    return Estimate(latest.t, latest.n, window_used, slope, n_future, wait_min, projected)


def is_within_target_window(est: Optional[Estimate]) -> bool:
    return est is not None and abs(est.projected - CFG.target) <= CFG.tol


def format_status_line(est: Estimate) -> str:
    w_str = f"w={est.window_used:.0f}m" if est.window_used else "w=--"
    diff_sec = (est.projected - CFG.target).total_seconds()
    sign = "+" if diff_sec >= 0 else "-"
    return (
        f"{est.t:%H:%M:%S}  N={est.n_now:5.1f}  "
        f"slope={est.slope:+.2f}/min({w_str})  "
        f"wait={est.wait_min:5.1f}min  "
        f"projected={est.projected:%H:%M}  "
        f"diff={sign}{abs(diff_sec) / 60:.0f}min"
    )


def print_params() -> None:
    print(
        f"[params] tables={CFG.tables:.0f}  avg_stay_min={CFG.avg_stay_min:.1f}  "
        f"windows={SLOPE_WINDOWS_MIN}  target={CFG.target:%H:%M}  "
        f"tol=±{int(CFG.tol.total_seconds() / 60)}min  "
        f"poll_interval={CFG.poll_interval}s  "
        f"tap_pos={CFG.tap_pos}  crop_box={CFG.crop_box}\n"
    )


def notify(msg: str) -> None:
    print(f"\n*** {msg} ***\n")
    try:
        subprocess.run(["notify-send", "予約しましょう", msg], check=False)
    except FileNotFoundError:
        pass


def load_calibration() -> None:
    """configure_regions.py が書き出した calib.json があれば tap_pos / crop_box に反映。"""
    if not CALIB_FILE.exists():
        return
    try:
        data = json.loads(CALIB_FILE.read_text())
        if "tap_pos" in data:
            CFG.tap_pos = tuple(data["tap_pos"])
        if "crop_box" in data:
            CFG.crop_box = tuple(data["crop_box"])
        print(f"[calib] {CALIB_FILE.name} を読み込みました: tap_pos={CFG.tap_pos} crop_box={CFG.crop_box}")
    except Exception as e:
        print(f"[calib] 読み込み失敗: {e}")


# ---------------- インタラクティブコマンド ----------------

HELP_TEXT = """\
[コマンド] (Enterで実行)
  avg_stay_min <分>          平均滞在時間を直接設定
  calibrate <N> <分>         実測(申込み時N, 実際の待ち分)からτを逆算して設定
  tables <卓数>               テーブル数を変更
  target <HH:MM>             目標着席時刻を変更
  tol <分>                    許容誤差を変更
  poll_interval <秒>          ポーリング間隔を変更
  reload                     今すぐ再取得
  tap <x> <y>                更新ボタンの座標を変更
  crop <x1> <y1> <x2> <y2>  OCR切り出し範囲を変更
  show                       現在のパラメータを表示
  help                       このヘルプを表示
"""

_reload_event = threading.Event()


def handle_command(line: str) -> None:
    parts = line.strip().split()
    if not parts:
        return
    cmd, *args = parts
    try:
        if cmd == "avg_stay_min" and args:
            CFG.avg_stay_min = float(args[0])
            print(f"[cfg] avg_stay_min -> {CFG.avg_stay_min:.1f}min")
        elif cmd == "calibrate" and len(args) >= 2:
            n_obs, wait_obs = float(args[0]), float(args[1])
            CFG.avg_stay_min = wait_obs * CFG.tables / n_obs
            print(f"[cfg] calibrate: N={n_obs} wait={wait_obs}min -> avg_stay_min={CFG.avg_stay_min:.1f}min")
        elif cmd == "tables" and args:
            CFG.tables = float(args[0])
            print(f"[cfg] tables -> {CFG.tables:.0f}")
        elif cmd == "target" and args:
            h, m = map(int, args[0].split(":"))
            CFG.target = CFG.target.replace(hour=h, minute=m, second=0, microsecond=0)
            print(f"[cfg] target -> {CFG.target:%H:%M}")
        elif cmd == "tol" and args:
            CFG.tol = timedelta(minutes=float(args[0]))
            print(f"[cfg] tol -> ±{args[0]}min")
        elif cmd == "poll_interval" and args:
            CFG.poll_interval = int(args[0])
            print(f"[cfg] poll_interval -> {CFG.poll_interval}s")
        elif cmd == "reload":
            _reload_event.set()
            print("[cfg] 次のサイクルをスキップして今すぐ再取得します")
        elif cmd == "tap" and len(args) >= 2:
            CFG.tap_pos = (int(args[0]), int(args[1]))
            print(f"[cfg] tap_pos -> {CFG.tap_pos}")
        elif cmd == "crop" and len(args) >= 4:
            CFG.crop_box = tuple(int(a) for a in args[:4])
            print(f"[cfg] crop_box -> {CFG.crop_box}")
        elif cmd in ("show", "s"):
            print_params()
        elif cmd in ("help", "h", "?"):
            print(HELP_TEXT)
        else:
            print(f"[cfg] 不明なコマンド: {line}  (helpで一覧表示)")
    except (ValueError, IndexError) as e:
        print(f"[cfg] パース失敗: {e}")


def command_listener() -> None:
    print(HELP_TEXT)
    while True:
        try:
            line = input()
        except EOFError:
            return
        handle_command(line)


# ---------------- 実行モード ----------------


def watch() -> None:
    print_params()
    threading.Thread(target=command_listener, daemon=True).start()
    history: List[Sample] = []
    while True:
        now = datetime.now()
        n = read_queue_count()
        if math.isnan(n):
            print(f"{now:%H:%M:%S}  OCR失敗、リトライ")
            time.sleep(15)
            continue
        history.append(Sample(now, n))
        est = evaluate(history, lookahead_min=LOOKAHEAD_MIN)
        next_t = datetime.now() + timedelta(seconds=CFG.poll_interval)
        print(format_status_line(est) + f"  next={next_t:%H:%M:%S}")
        if is_within_target_window(est):
            notify(f"今が申込みタイミングです(予測着席 {est.projected:%H:%M})")
        _reload_event.clear()
        _reload_event.wait(timeout=CFG.poll_interval)


def simulate(peak_n: float = 60) -> None:
    """実機なしで決定ロジックを試すための合成データ(山型カーブ+ノイズ)シミュレーション。"""
    base = datetime(2026, 1, 1, 17, 0, 0)
    if CFG.target.date() != base.date():
        CFG.target = base.replace(hour=19, minute=0)
    print_params()
    history: List[Sample] = []
    for i in range(0, 181, 5):
        t = base + timedelta(minutes=i)
        base_n = peak_n * (i / 90) if i <= 90 else peak_n * (1 - (i - 90) / 90)
        noise = 3 * math.sin(i / 7)
        history.append(Sample(t, max(base_n + noise, 0)))
        est = evaluate(history, lookahead_min=LOOKAHEAD_MIN)
        line = format_status_line(est)
        if is_within_target_window(est):
            print(line + "  <-- ここで申込み")
            break
        print(line)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="順番指定予約 監視ツール")
    ap.add_argument("--target-time", default=None, help="着席目標時刻 HH:MM (省略時は対話入力)")
    ap.add_argument("--avg-stay-min", type=float, default=35.0, help="初期平均滞在時間(分)")
    ap.add_argument("--tables", type=float, default=30.0, help="テーブル数")
    ap.add_argument("--tol", type=float, default=5.0, help="許容誤差(分)")
    ap.add_argument("--simulate", action="store_true", help="実機なしでロジックを試す")
    args = ap.parse_args()

    target_str = args.target_time
    if target_str is None:
        target_str = input("着席目標時刻を入力してください (HH:MM): ").strip()
    h, m = map(int, target_str.split(":"))
    target_dt = datetime.now().replace(hour=h, minute=m, second=0, microsecond=0)
    CFG.target = target_dt
    CFG.avg_stay_min = args.avg_stay_min
    CFG.tables = args.tables
    CFG.tol = timedelta(minutes=args.tol)

    if not args.simulate:
        if not CALIB_FILE.exists():
            print(
                """
                calib.json が見つかりません。
                先に configure_regions.py を実行してキャリブレーションを行ってください。
                """
            )
            sys.exit(1)
    load_calibration()

    if args.simulate:
        simulate()
    else:
        watch()
