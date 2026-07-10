# musx2mscz

Finale Notation File (.musx) を MuseScore ファイル (.mscz) に変換するコマンドラインツールです。

## 必要環境

- macOS + [MuseScore Studio 4](https://musescore.org/)（変換エンジンとして使用）
- [uv](https://docs.astral.sh/uv/)

## 使い方

```bash
# 1ファイル変換
uv run musx2mscz samples/musx/score.musx -o out/score.mscz

# ディレクトリ一括変換
uv run musx2mscz samples/musx -o out/

# 中間ファイル（.enigmaxml / .musicxml / .mss）も保存
uv run musx2mscz samples/musx/score.musx -o out/score.mscz --keep
```

## 変換の仕組み

1. `.musx`（zip コンテナ）から `score.dat` を取り出し、ストリーム暗号を復号して EnigmaXML を得る
2. EnigmaXML を解析し MusicXML を生成（音符・パーカッションマップ・強弱/速度記号・フォント情報）
3. 文書オプションから MuseScore スタイル（.mss）を生成（音楽フォント、五線サイズ、テキストスタイル）
4. MuseScore の CLI で MusicXML + スタイルを .mscz へ変換
5. .mscz を後処理（Garritan ARIA Player 等の音源バインド）

## クレジット

- EnigmaXML の復号および変換ロジックの一部は [musx2mxl](https://github.com/joris-vaneyghen/musx2mxl) (MIT) に基づいています
- パーカッションノートタイプ表は [musxdom](https://github.com/rpatters1/musxdom) (MIT) 由来です
