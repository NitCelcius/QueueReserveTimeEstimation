# ReserveTimingEstimation

某順番指定予約アプリの待ち人数をADB経由でスクショして監視し、指定した着席目標時刻に合わせて「今が順番指定予約するタイミング」を推定する。

## どんなときに使う？

あるお店に行きたくて来店希望の時間が決まっている。時間指定予約はできないが、順番予約ができるお店において、なるだけ順番予約による待ち時間を減らしたいので、いつ順番指定予約を行えばいいか知りたいとき。
※ ただし、一度並んでみて待ち行列がどれだけの速度で減るかを調べ `avg_stay_min` に入れる必要があります。あるいは、お店のテーブル数と平均滞在時間がわかれば、一定時間ごとの退店数を算出してこれを参考にします

※ 完全に vibe coding した

## インストール

前提: Python 3.13+、[uv](https://github.com/astral-sh/uv)、[Tesseract OCR](https://github.com/tesseract-ocr/tesseract)、[adb](https://developer.android.com/tools/adb)が使える状態であること。

```bash
uv sync
```

Tesseractのインストール先が `C:\Program Files\Tesseract-OCR\tesseract.exe` でない場合は `main.py` 冒頭の `tesseract_cmd` を変更してください。
※ べつに Windows でない場合にはコマンドラインを書き換えてください。

## 使い方

### 1. キャリブレーション(初回のみ)

adbにAndroid端末を接続した状態で以下を実行します。

```bash
uv run python configure_regions.py
```

「更新ボタンのタップ位置」と「待ち人数のOCR切り出し範囲」を視覚的に指定します。
設定は `calib.json` に保存され、次回以降の起動時に自動で読み込まれます。

### 2. 監視の開始

```bash
uv run python main.py --target 19:00
```

オプション:

| フラグ | デフォルト | 説明 |
|--------|-----------|------|
| `--target` | (必須) | 着席目標時刻 HH:MM |
| `--tau` | 35 | 初期τ: 1卓あたり平均滞在時間(分) |
| `--tables` | 30 | テーブル数 |
| `--tol` | 5 | 許容誤差(分) |
| `--simulate` | — | 実機なしでロジックを確認(合成データ) |

### 3. 実行中のコマンド

起動後、標準入力にコマンドを入力することでパラメータを再起動なしに変更できます。

```
tau <分>                   τを直接変更
calibrate <N> <分>         実測(申込み時のN, 実際に待った分)からτを逆算
tables <卓数>               テーブル数を変更
target <HH:MM>             目標着席時刻を変更
tol <分>                    許容誤差を変更
poll_interval <秒>          ポーリング間隔を変更
reload                     即座に再取得
tap <x> <y>                更新ボタン座標を変更
crop <x1> <y1> <x2> <y2>  OCR切り出し範囲を変更
show                       現在のパラメータ表示
help                       コマンド一覧
```
