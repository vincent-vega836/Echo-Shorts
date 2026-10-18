# Atlas Grey — "facts" YouTube Shorts kanalı işletme kılavuzu

Bu belge, 20 soruluk planlama anketinde verilen kararları ve bunları uygulayan
araçları anlatır. Kod: `shorts_cli.py` (+ `app/services/shorts_*.py`).

## Kararlar

| # | Konu | Karar |
| --- | --- | --- |
| 1 | Kanal kimliği | Sabit anlatıcı karakteri: **Atlas Grey** (kanal adı ve anlatıcı) |
| 2 | Niş | Bilgi / ilginç gerçekler |
| 3 | Kitle | ABD + global İngilizce |
| 4 | Ton | Gizemli, merak uyandıran |
| 5 | Alt konular | Psikoloji & beyin, karanlık tarih & gizemler, uzay — dönüşümlü; 30 gün sonra en iyisine odak |
| 6 | Süre | 20-35 sn |
| 7 | Kanca | Şok edici ama doğru iddia (ilk 2 sn) |
| 8 | Yapı | Video başına tek, derin gerçek |
| 9 | Altyazı | Kelime kelime vurgulu |
| 10 | Görüntü | Pixabay (Pexels yeni anahtar vermiyor); Coverr eklenirse dönüşümlü |
| 11 | Ses | Ücretsiz Edge sesi `en-US-AndrewMultilingualNeural` (sıcak, doğal anlatıcı; dinleyerek seçildi); ElevenLabs istenmedi |
| 12 | Müzik | Kısık gerilim/ambient |
| 13 | Saat | Her gün 12:30 New York (= 19:30 İstanbul) |
| 14 | Platform | YouTube + TikTok |
| 15 | Kapanış | Başa bağlanan döngü cümlesi |
| 16 | Doğruluk | Her iddia için kaynak; Mila kontrol eder, kaynaklar açıklamada |
| 17 | Onay | Haftalık toplu onay (Pazar) |
| 18 | Konu seçimi | Mila + trend verisi |
| 19 | Rapor | Haftalık Cuma raporu |
| 20 | Ek gelir | Aylık uzun video derlemesi |
| — | Sıklık / bütçe | Günde 1 Short, ayda ~0 $ (senaryo Claude aboneliğiyle) |

## Haftalık akış

```
Pazar   Mila trendlerden 7 konu seçer → plan.json → `batch` 7 video üretir (kuyrukta "pending")
        Mila 7 videoyu + başlık/kaynakları #brand-social'da gösterir
        Doğukan Bey onaylar/reddeder → Mila yalnızca bu talimatla `approve` / `reject` çalıştırır
Her saat `publish-due` (cron/launchd) zamanı gelen onaylı videoyu YouTube + TikTok'a yükler
Cuma    Mila haftalık raporu yazar (izlenme, abone, en iyi/kötü video, maliyet, YPP ilerlemesi)
Ay sonu `compile --last 12` en son 12 Short'tan 16:9 uzun video üretir (onaylanınca elle yüklenir)
```

Bir render başarısız olursa o güne slot verilmez; sıradaki başarılı video
boşluğu doldurur. Yükleme sonucu belirsizse (zaman aşımı) video tekrar
gönderilmez, `needs_check` olur: Upload-Post panelinden kontrol edin.

## Kurulum

`config.toml`:

```toml
[app]
llm_provider = "claude_code"   # senaryolar Claude aboneliğiyle; API anahtarı gerekmez
video_source = "pixabay"
pixabay_api_keys = ["..."]
pixabay_crop_to_fit = true      # Pixabay'da dikey video az; yatay 1080p+ klipleri kırpıp kullanır
# coverr_api_keys = ["..."]    # isteğe bağlı ikinci görüntü kaynağı
subtitle_provider = "edge"     # Edge sesi kelime zamanı verir; Whisper gerekmez
edge_tts_timeout = 90          # varsayılan 30 sn, Mac başka video işlerken yetmiyor
max_concurrent_tasks = 1       # tek Mac: videolar sırayla üretilir, fazlası kuyrukta bekler
upload_post_enabled = true
upload_post_api_key = "..."
upload_post_username = "..."
upload_post_youtube_privacy_status = "public"

[slack]
profile = "facts"
video_sources = ["pixabay"]    # Coverr eklenirse ["pixabay", "coverr"]
publish_platforms = ["youtube", "tiktok"]
queue_path = "storage/shorts/queue.json"
```

Daha doğal bir ses istenirse ElevenLabs sonradan eklenebilir:
`[elevenlabs] api_key`, `[slack] elevenlabs_voice = "elevenlabs:<voice_id>:<isim>"`
ve bu durumda `subtitle_provider = "whisper"` ile `[whisper] model_size = "small.en"`
gerekir (ElevenLabs kelime zamanı vermez; araç ayar eksikse uyarır).
Upload-Post'ta YouTube ve TikTok hesaplarını hesap sahibi bağlar; ajan şifre görmez.

Sunucu (MoneyPrinterTurbo API) üretim boyunca açık kalmalı. macOS'ta kendi
Terminal penceresinde, ön planda çalıştırın; `&` ile arka plana atılırsa
alt süreçler terminale dokunduğunda macOS onu dondurur:

```bash
osascript -e 'tell application "Terminal" to do script "cd ~/Echo-Shorts && uv run python main.py"'
```

Saatlik yayın işi (macOS, `crontab -e`):

```cron
5 * * * * cd ~/Echo-Shorts && /opt/homebrew/bin/uv run python shorts_cli.py publish-due >> storage/shorts/publish.log 2>&1
```

Mac uykudayken cron çalışmaz; kaçan videolar Mac uyanınca ilk çalışmada yüklenir.

## Komutlar

```bash
uv run python shorts_cli.py batch --plan plan.json --out storage/shorts/videos
uv run python shorts_cli.py queue --status pending
uv run python shorts_cli.py approve --all-pending      # ya da: approve <id> <id>
uv run python shorts_cli.py reject <id>
uv run python shorts_cli.py publish-due --dry-run       # neyin yükleneceğini göster
uv run python shorts_cli.py compile --last 12 --out storage/shorts/long/2026-10.mp4
```

Plan dosyası örneği: [`docs/shorts/plan.example.json`](shorts/plan.example.json).
Her öğede `text` Slack isteğiyle aynı biçimdedir; `ref:` satırları kaynak
linkleridir (en fazla 5, açıklamaya eklenir, seslendirilmez). `---` sonrası
yazılırsa senaryo olduğu gibi kullanılır.

## Maliyet (ayda ~30 video)

| Kalem | Tahmin |
| --- | --- |
| Senaryo (Claude aboneliği, `claude_code`) | 0 $ ek maliyet |
| Ses (Edge TTS), görüntü (Pixabay), MoneyPrinterTurbo | 0 $ |
| Upload-Post | Ücretsiz katman yetmezse ücretli plan |

## Gerçekçi beklenti ve riskler

- YouTube Partner Programı: 1.000 abone + 90 günde 10M Shorts izlenmesi
  **veya** 1.000 abone + 4.000 saat uzun video izlenmesi. Aylık derlemeler
  ikinci yolu besler.
- YouTube "inauthentic / seri üretilmiş" içeriği para kazanmadan çıkarır.
  Korunma: tek derin gerçek, özgün anlatım, kaynak, insan onayı, aynı kalıbı
  tekrar etmemek. Yapay ses kullanımı yüklemede `containsSyntheticMedia` ile
  beyan edilir.
- İlk 2-3 ay gelir büyük ihtimalle sıfıra yakındır; hedef önce kitle.
