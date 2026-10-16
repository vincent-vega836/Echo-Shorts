# Slack'ten YouTube Shorts üretimi (Mila entegrasyonu)

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

2. **Mila'yı izinli yapın** — Mila'nın bir mesajının bot ID'sini (`B…`) alın
   (mesaj → *Copy link* ile API'de `bot_id` alanı) ve `allowed_bot_ids`'e yazın.
   İzin listesinde olmayan botların `short:` mesajları yok sayılır.

3. **Yapılandırma** — `config.toml` içine (ya da `SLACK_<ANAHTAR>` ortam
   değişkenleri olarak):

   ```toml
   [slack]
   allowed_channels = ["C0BRANDSOCIAL"]   # #brand-social kanal ID'si
   allowed_bot_ids = ["B0MILA"]
   approver_user_ids = ["U0DOGUKAN"]      # boşsa herkes onaylayabilir
   posting_slots = ["12:30", "19:00", "21:00"]
   ```

   Paylaşım saatlerini ekibin kullandığı İstanbul en iyi saat verisine göre
   güncelleyin; varsayılanlar yalnızca başlangıç noktasıdır.

4. **Çalıştırma**

   ```bash
   python main.py                                  # MPT API
   uv sync --extra slack
   SLACK_BOT_TOKEN=xoxb-... SLACK_APP_TOKEN=xapp-... uv run python slack_bot.py
   ```

   Docker ile: `SLACK_BOT_TOKEN=... SLACK_APP_TOKEN=... docker compose --profile slack up -d`

## Notlar

- `[app].api_key` ayarlıysa bot aynı anahtarı `x-api-key` başlığıyla gönderir;
  video indirme yalnızca `/tasks/` yolunu ve yapılandırılmış API adresini kullanır.
- "Yeniden üret" düğmesi istekleri bellekte tutar; bot yeniden başlarsa eski
  mesajlarda isteği tekrar göndermek gerekir.
- Slack dosya yükleme sınırları geçerlidir; Shorts (≤60 sn, 1080p) bu sınırın
  çok altındadır.
