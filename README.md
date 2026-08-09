# Video Downloader

YouTube・Instagram・TikTok・X など約1,800サイト([yt-dlp](https://github.com/yt-dlp/yt-dlp) 対応サイト)から動画を高解像度でダウンロードできる、ローカル実行のWebアプリです。Whisperによる文字起こしにも対応しています。

## 機能

- **画質選択**: 最高画質(自動)/ 2160p〜480p / 音声のみ(m4a・mp3・opus・wav)
- **複数URL・プレイリスト対応**: 1行に1URLで一括ダウンロード。プレイリストは自動で個別ジョブに展開(上限100件)
- **同時ダウンロード**(1〜5並列)と**キャンセル**(中断ファイルは自動削除)
- **ダウンロード履歴**: サムネイル・日時・サイズ付き。Finderで表示ボタンあり
- **字幕保存**(srt、日/英)・**サムネイル保存**(jpg)
- **文字起こし(Whisper)**: 動画の音声をローカルAIでテキスト化(.txt とタイムスタンプ付き .srt)。言語選択に応じて最適モデルを自動選択(英語: large-v3-turbo / 日本語・混在・自動: large-v3)。字幕がないサイトの動画でも抽出可能 ※Apple Silicon Macのみ
- **区間指定ダウンロード**: 「0:30」〜「2:00」のように指定して切り抜き
- **ブラウザCookie連携**: Instagramの非公開投稿・メンバー限定動画など(Chrome / Safari / Firefox / Edge / Brave)
- **クリップボード自動検出**: URLをコピーした状態でアプリを開くと自動入力
- **設定**: 保存先フォルダ・ファイル名テンプレート(yt-dlp出力テンプレート形式)

## 必要なもの

- macOS(文字起こし機能はApple Silicon専用。それ以外の機能はIntel Macでも動作)
- Python 3.9+
- ffmpeg

```bash
brew install ffmpeg
```

## セットアップ

```bash
git clone https://github.com/selo-git/video-downloader.git
cd video-downloader
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## 起動

```bash
.venv/bin/python app.py
```

ブラウザで http://localhost:3456 を開きます。ダウンロードしたファイルは既定で `~/Downloads/VideoDownloader/` に保存されます(設定タブで変更可能)。

## 使い方

1. 動画・プレイリストのURLを貼り付ける(複数の場合は1行に1つ)
2. 画質・オプション(字幕 / サムネイル / 文字起こし / 区間指定)を選ぶ
3. 「ダウンロード」を押すとキューに追加され、進捗が表示される

### 文字起こし

「文字起こし (Whisper)」にチェックを入れ、動画の言語(自動判定 / 日本語 / 英語 / 日英混在)を選ぶと、ダウンロード後に音声をテキスト化して `.txt`(全文)と `.transcript.srt`(タイムスタンプ付き)を保存します。初回はモデルのダウンロード(1.6〜3GB)が走ります。

### ログインが必要なサイト

Instagramの非公開投稿などは、設定タブ「ブラウザのCookieを使う」で普段ログインしているブラウザを選んでください。Chromeの場合、初回にmacOSのキーチェーン許可が求められることがあります(ブラウザを完全終了した状態で実行すると成功しやすいです)。

## トラブルシューティング

サイト側の仕様変更でダウンロードできなくなった場合は yt-dlp を更新してください:

```bash
.venv/bin/pip install -U yt-dlp
```

## 注意事項

本ツールは、ご自身がアップロードした動画や、権利者がダウンロードを許可しているコンテンツの保存を目的としています。各サイトの利用規約と著作権法を遵守してご利用ください。

## ライセンス

[MIT](LICENSE)
