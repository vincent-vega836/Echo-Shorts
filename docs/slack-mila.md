# Slack'ten YouTube Shorts üretimi (Mila entegrasyonu)

İki kullanım yolu var:

- **A. Doğrudan Mila (önerilen, ek Slack uygulaması gerekmez):** Mila
  `shorts_cli.py` aracını çalıştırır, videoyu ve YouTube taslağını alır, kendi
  Slack kimliğiyle thread'e yükler.
- **B. Ayrı "Echo Shorts" botu:** `/short` komutu ve düğmeli onay kartı için
  workspace'te yeni bir Slack uygulaması gerekir (aşağıda "Ayrı bot" bölümü).

## A. Doğrudan Mila

Mila'nın çalıştığı makinede MPT API açık olmalı (`uv run python main.py`).
Mila, isteği Slack'teki biçimle aynen verir:

```bash
uv run python shorts_cli.py --out ./shorts "short: Sakin yazılım planlaması dil:tr"
# çok satırlı istek / hazır senaryo için stdin:
printf 'short: Sakin planlama\nses: erkek\n---\nHazır senaryo metni.' | uv run python shorts_cli.py --out ./shorts
```

Çıktı tek bir JSON'dur (ilerleme stderr'e yazılır):

```json
{
  "task_id": "…", "subject": "Sakin yazılım planlaması", "language": "tr",
  "videos": ["/…/shorts/short-…-1.mp4"],
  "script": "…", "title": "Sakin yazılım planlaması #Shorts",
  "description": "…\n\nmadebyecho.xyz", "tags": ["shorts", "echo", "…"],
  "posting_slots": [{"at": "2026-10-17T19:00:00+03:00", "label": "Cmt 17.10 19:00"}],
  "warnings": []
}
```

Çıkış kodları: `0` hazır, `2` geçersiz istek/ayar, `1` üretim hatası
(her durumda stdout'ta `{"error": "…"}` ya da sonuç JSON'u bulunur).
API adresi, anahtar, saat dilimi, paylaşım saatleri ve preset `[slack]`
bölümünden okunur; Slack token'ı gerekmez.

**Mila'nın talimatına eklenecek metin:**

> Doğukan Bey veya ekip bir YouTube Short istediğinde `shorts_cli.py`
> aracını isteği olduğu gibi vererek çalıştır. Senaryoyu sen yazdıysan
> `---` satırından sonra ekle. Çıkan `videos` dosyalarını thread'e yükle;
> `title`, `tags` ve `posting_slots` bilgisini ayrı mesajda, `description`
> metnini caption olarak ayrı bir mesajda ver. Asla yayınlama; yayın için
> onay ve saat iste. `error` dönerse hatayı kısaca bildir, otomatik tekrar
> deneme.

## B. Ayrı bot

`slack_bot.py`, Slack'teki istekleri MoneyPrinterTurbo API'sine iletir, üretilen
dikey videoyu aynı thread'e yükler ve bir insanın onayına sunar.

```
Mila / ekip  ──"short: konu"──▶  Slack bot  ──POST /api/v1/videos──▶  MPT API
     ▲                              │  ◀──GET /api/v1/tasks/{id}──────────┘
     └── thread: video + başlık + etiketler + önerilen saatler + [Onayla][Yeniden üret][Reddet]
```

Yayın bilinçli olarak insanda kalır: ajanlara hesap girişi verilmez. Bot,
YouTube başlığı/açıklaması/etiket taslağını ve İstanbul saatine göre sonraki üç
paylaşım saatini önerir; yayını hesap sahibi yapar.

## İstek biçimi

Slash komutu:

```
/short Yazılım projelerinde sakin planlama dil:tr ses:erkek
```

Bota mention (`@Echo Shorts Sakin planlama`) ya da izinli bir ajanın (Mila)
kanala yazdığı mesaj:

```
short: Yazılım projelerinde sakin planlama
dil: tr
format: dikey
adet: 2
---
İsteğe bağlı hazır senaryo. Yazılırsa LLM adımı atlanır ve bu metin
olduğu gibi seslendirilir. Mila metni kendisi yazacaksa bu bölümü kullanır.
```

| Seçenek | Değerler | Varsayılan |
| --- | --- | --- |
| `dil` / `lang` | `tr`, `en`, `de` | `tr` |
| `ses` / `voice` | `kadın`, `erkek` | `kadın` |
| `format` / `aspect` | `dikey` (9:16), `yatay` (16:9), `kare` (1:1) | `dikey` |
| `adet` / `count` | 1–5 | 1 |
| `kaynak` / `source` | `pexels`, `pixabay`, `ofox`, … | config'deki `video_source` |

Senaryo verilmezse ECHO marka tonu (sakin, düzenli, abartısız; 2 saniyelik
kanca; madebyecho.xyz ile biten tek çağrı) LLM'e ek gereksinim olarak gönderilir.
Altyazı fontu Türkçe/Almanca karakterleri tam destekleyen `BeVietnamPro-Bold.ttf`.

## Kurulum

1. **Slack uygulaması** — https://api.slack.com/apps → *Create New App* →
   *From a manifest*, aşağıdaki manifest'i yapıştırın:

   ```yaml
   display_information:
     name: Echo Shorts
   features:
     bot_user:
       display_name: Echo Shorts
     slash_commands:
       - command: /short
         description: YouTube Shorts üret
         usage_hint: "<konu> dil:tr ses:kadın"
   oauth_config:
     scopes:
       bot:
         - app_mentions:read
         - channels:history
         - groups:history
         - chat:write
         - commands
         - files:write
   settings:
     event_subscriptions:
       bot_events:
         - app_mention
         - message.channels
         - message.groups
     interactivity:
       is_enabled: true
     socket_mode_enabled: true
   ```

   *Basic Information → App-Level Tokens* altında `connections:write` kapsamlı
   bir token (`xapp-…`) oluşturun, uygulamayı workspace'e kurup bot token'ını
   (`xoxb-…`) alın. Botu `#brand-social` kanalına ekleyin.

2. **Mila'yı izinli yapın** — Mila Novak'ın bot ID'si `B0C87P81QBG`; aşağıdaki
   örnekte zaten yazılı. İzin listesinde olmayan botların `short:` mesajları
   yok sayılır.

3. **Yapılandırma** — `config.toml` içine (ya da `SLACK_<ANAHTAR>` ortam
   değişkenleri olarak):

   ```toml
   [slack]
   allowed_channels = ["C0C86NYJ0T0"]     # #brand-social
   allowed_bot_ids = ["B0C87P81QBG"]      # Mila Novak
   approver_user_ids = ["U0C4G7H5B6V"]    # Doğukan; boşsa herkes onaylayabilir
   posting_slots = ["12:30", "19:00", "21:00"]
   ```

   Paylaşım saatlerini ekibin kullandığı İstanbul en iyi saat verisine göre
   güncelleyin; varsayılanlar yalnızca başlangıç noktasıdır.

4. **Çalıştırma**

   ```bash
   python main.py                                  # MPT API
   SLACK_BOT_TOKEN=xoxb-... SLACK_APP_TOKEN=xapp-... \
     uv run --with slack-bolt==1.30.0 python slack_bot.py
   ```

   Docker ile (`slack-bolt` imaja derleme sırasında kurulur):

   ```bash
   SLACK_BOT_TOKEN=xoxb-... SLACK_APP_TOKEN=xapp-... docker compose --profile slack up -d --build
   ```

## Notlar

- `[app].api_key` ayarlıysa bot aynı anahtarı `x-api-key` başlığıyla gönderir;
  video indirme yalnızca `/tasks/` yolunu ve yapılandırılmış API adresini kullanır.
- Mila'nın mesajı hem `short:` ile başlayıp hem botu mention etse bile tek
  video üretilir; iki kişi aynı anda düğmeye bassa da tek karar işlenir.
- "Yeniden üret" düğmesi son 200 isteği bellekte tutar; bot yeniden başlarsa
  eski mesajlarda isteği tekrar göndermek gerekir.
- Ayar hataları (geçersiz dil, saat dilimi, paylaşım saati) bot açılırken
  anahtar adıyla raporlanır. Beklenmeyen hatalar Slack'e değil loglara yazılır.
- Kod: `app/services/slack_shorts.py` (ayrıştırma, preset, API istemcisi),
  `app/services/slack_shorts_bot.py` (Slack davranışı, Slack SDK'sız test
  edilir), `slack_bot.py` (yalnızca başlatıcı).
- Slack dosya yükleme sınırları geçerlidir; Shorts (≤60 sn, 1080p) bu sınırın
  çok altındadır.
