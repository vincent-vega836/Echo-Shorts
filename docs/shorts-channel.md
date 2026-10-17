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
| 10 | Görüntü | Pexels + Pixabay dönüşümlü |
| 11 | Ses | ElevenLabs, derin erkek anlatıcı |
| 12 | Müzik | Kısık gerilim/ambient |
| 13 | Saat | Her gün 12:30 New York (= 19:30 İstanbul) |
| 14 | Platform | YouTube + TikTok |
| 15 | Kapanış | Başa bağlanan döngü cümlesi |
| 16 | Doğruluk | Her iddia için kaynak; Mila kontrol eder, kaynaklar açıklamada |
| 17 | Onay | Haftalık toplu onay (Pazar) |
| 18 | Konu seçimi | Mila + trend verisi |
| 19 | Rapor | Haftalık Cuma raporu |
| 20 | Ek gelir | Aylık uzun video derlemesi |
| — | Sıklık / bütçe | Günde 1 Short, ayda ≤10 $ |

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
pexels_api_keys = ["..."]
pixabay_api_keys = ["..."]
# ElevenLabs kelime zamanı vermez; kelime kelime altyazı için Whisper şart.
subtitle_provider = "whisper"
upload_post_enabled = true
upload_post_api_key = "..."
upload_post_username = "..."
upload_post_youtube_privacy_status = "public"

[whisper]
model_size = "small.en"   # İngilizce için hızlı ve yeterli; large-v3 CPU'da çok yavaş

[elevenlabs]
api_key = "..."

[slack]
profile = "facts"
elevenlabs_voice = "elevenlabs:<voice_id>:<isim>"   # derin erkek anlatıcı
video_sources = ["pexels", "pixabay"]
publish_platforms = ["youtube", "tiktok"]
queue_path = "storage/shorts/queue.json"
```

ElevenLabs ses kimliğini (voice_id) ElevenLabs Voice Library'den seçin; WebUI'nin
ses listesi de `elevenlabs:<id>:<isim>` biçiminde gösterir. Upload-Post'ta
YouTube ve TikTok hesaplarını hesap sahibi bağlar; ajan şifre görmez.

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
| ElevenLabs Starter (30 × ~500 karakter ≈ 15 bin karakter) | ~5 $ |
| Senaryo LLM'i | <1 $ |
| Pexels, Pixabay, Whisper (yerel), MoneyPrinterTurbo | 0 $ |
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
