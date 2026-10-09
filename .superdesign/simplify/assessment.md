# Operational Inbox yeniden değerlendirmesi

9 Ekim 2026 · Local `main` checkout · Product Design audit + Superdesign proposal

**Implementation update:** Onaylanan yön 9 Ekim 2026'da local `main` üzerinde uygulandı. Aşağıdaki audit ve prototype bölümleri değişiklik öncesi değerlendirmeyi kaydeder. Güncel sonuçlar [implementation QA](/Users/onurmatik/Projects/OperationalInbox/design-qa.md) dosyasında; uygulama özeti bu belgenin sonundadır.

Ürünü **project domain'leri için agent tarafından kullanılan bir email servisi** olarak konumlandırmayı öneriyorum. İnsan için ana ekran, gelen ve gönderilen maillerin okunabilir conversation geçmişi olmalı. Agent setup, domain configuration, account controls ve delivery sorunları gerektiğinde açılmalı.

İki temel iş aynı derecede kolay başlamalı: support/contact mail almak ve müşteriye tek bir mail göndermek. Bu değerlendirmede CRM, contact database, campaign, task workflow veya uygulama içinde ayrı bir AI chat eklenmesi önerilmiyor.

[Tıklanabilir Superdesign proposal](https://p.superdesign.dev/draft/e21c2fe9-0642-4f5c-8ed6-629c410e7aac) · [Mevcut project canvas](https://superdesign.dev/teams/05ba2391-e820-46da-b063-3a4d3210f0b9/projects/04a87f91-aea0-4c8f-b358-afc8e0bbf553?node=draft-variant-e21c2fe9-0642-4f5c-8ed6-629c410e7aac) · [Kaydedilen prototype HTML](/Users/onurmatik/Projects/OperationalInbox/.superdesign/simplify/proposal.html)

Draft version 9, bu incelemedeki önerilen yöndür. Aynı draft'ın version 3 geçmişinde mevcut Inbox baseline'ı bulunur. Prototype yalnızca kurmaca veriler kullanır; domain bağlamaz, DNS değiştirmez veya mail göndermez.

## Önerilen ürün yapısı

| Alan | Önerilen davranış |
| --- | --- |
| Conversations | Default landing. Tüm owner domain'lerinin gelen ve gönderilen conversation'ları. |
| Inbox / Sent / Archive | Aynı liste için görünüm seçenekleri. Starred ve Trash, More altında. |
| Domain | Listede açık bir filter. Conversation işleminin domain'i kaydın kendisinden türetilir. |
| Search | Her zaman görünür. Tag, security, mailbox ve delivery filters ihtiyaç halinde açılır. |
| New email | From, To, Subject, Message. Sending-ready domain üzerinde herhangi bir local part kullanılabilir. |
| Conversation | Okunabilir timeline. Delivery state ilgili mailin yanında. Tag edit ve RFC metadata Details altında. |
| Draft | Agent bir revision kaydettiğinde inline review. Boş draft paneli sürekli gösterilmez. |
| Domains | İkinci ana ekran. Hostname, Receiving, Sending ve gerekiyorsa tek next action. |
| Add domain | İlk karar: Receive email / Send email / Both. Sadece sending isteyen kullanıcı için receiving setup zorunlu değildir. |
| Connect your agent | Tek public installation prompt. Access yönetimi ve ayrıntılı docs ikincil bağlantılar. |
| Settings | Plan, retention, API access, account sending pause ve audit tek girişte. |

Mailbox'lar şu an recipient geçmişinden türetiliyor. Bu ürün için yeni bir `Mailbox`, `Project` veya `Contact` modeli eklemek gerekmiyor. Bir domain'in receiving configuration'ı hazır olduğunda support@ ve contact@ gibi adresler çalışabilir; UI bunu ilk mail gelmeden de açıkça anlatmalı. Domain test adresleri normal mailbox navigation'ına taşınmamalı.

Inbox, arşivlenmemiş incoming conversation'ları gösterir. Sent, outbound mail içeren conversation'ları gösterir; bir conversation Archive'a taşınsa da gönderilmiş mail Sent'te bulunabilmelidir. Archive iki yöndeki arşivlenmiş conversation'ları içerir. Trash bunların dışında tutulur. Bu görünümler aynı history üzerinde filter'dır; ayrı storage workflow'leri değildir.

## Current-flow audit

Ekranlar mevcut local source'un Django view ve template'lerinden, ayrı ve geçici bir SQLite database üzerinde kurmaca `*.example` mail fixture'ları ile üretildi. Üretim verisi bu rapora veya Superdesign'e gönderilmedi. Desktop capture 1440×900, mobile capture 390×844. Screenshot'lar bu audit sırasında kaydedildi ve açılarak incelendi. Bunlar mevcut UI render'larıdır; DNS ve mail provider işlemleri çalıştırılmadı.

| Adım | İncelenen akış | Durum | En önemli bulgu |
| --- | --- | --- | --- |
| 1 | Domain ekleme | Sade başlangıç, yanlış kapsam | Form yalnızca “Domain to receive email at” diyor. Sending ihtiyacı için ayrı başlangıç yok. |
| 2 | Domain listesi ve DNS setup | İşlevler anlaşılır, tekrar fazla | Receiving/Sending zaten ayrı, fakat route, provider ve DNS açıklamaları birçok bölgede tekrarlanıyor. |
| 3 | Agent bağlantısı | Güvenilir yönlendirme, uzun ekran | Public install prompt yeterli başlangıç; skill/MCP/prompt karşılaştırması, workflow gallery ve scope tablosu ana akışı uzatıyor. |
| 4 | Overview ve Inbox | Yapısal sadeleştirme gerekli | İki farklı mail listesi. Overview cross-domain, Inbox tek domain. “Inbox” yanındaki sayı quarantine, domain sayıları unread; anlamları görünür değil. |
| 5 | Conversation okuma | Mail okunabilir, hierarchy zayıf | UUID, tag formu ve boş draft paneli ana okuma alanını bölüyor. |
| 6 | New email ve delivery takibi | Sending çalışıyor, history dağınık | Compose ayrı sayfa; delivery ayrı Outbox. Outbox default 24h filter eski gönderimleri ilk bakışta saklıyor. |
| 7 | Archive | Reversible ve tutarlı, domain'e bağlı | Archive bütün hesabın arşivi gibi davranmıyor; seçilmiş domain'in görünümü. |

Mobil Inbox'ta search/tag/security paneli yaklaşık ekranın yarısını dolduruyor. Outbox'ta ilk gönderim satırından önce dört metric tile ve dört filter alanı geliyor. Outbox table minimum 920px; 390px ekranın içinde yatay scroll gerekiyor. Page seviyesinde horizontal overflow gözlenmedi; bu olumlu, fakat mail geçmişinin kendisi hâlâ zor okunuyor.

Visible labels, text ile belirtilen quarantine state, native controls, desktop focus styling ve mobile row action target'ları mevcut sistemin güçlü tarafları. Tam accessibility compliance doğrulanmadı. Screen reader, bütün keyboard yolları, zoom, contrast ölçümü ve production error recovery ayrı verification gerektiriyor.

Adım 2'deki yoğun DNS tabloları ve adım 3'teki uzun agent setup, kullanıcıdan fazla tarama gerektiriyor. Adım 4'te aynı navigation alanında farklı anlamlı sayılar bulunduğu için state'i anlama yalnızca sayılardan mümkün değil. Adım 5'te uzun UUID ve küçük secondary metadata okuma sırasını bozuyor. Adım 6'nın mobile table'ı yatay navigation gerektiriyor. Bunlar ekranlarda gözlenen okuma ve navigation sorunlarıdır; contrast veya screen reader hatası olarak ölçülmüş değildir.

## Kodda doğrulanan üç davranış

1. **Domain session birden fazla tab'de kırılabiliyor.** Aynı client session'ında önce A, sonra B domain Inbox'ı açıldı. A conversation'ı explicit domain olmadan açıldığında `404`; `?domain=A` ile `200`. [conversation_detail](/Users/onurmatik/Projects/OperationalInbox/inbox/views.py:1175) ve conversation actions session'daki domain'e bağlı. Owner-scoped conversation lookup ile domain kayıttan bulunmalı. Domain scope ve ownership checks korunmalı.
2. **Sending receiving'e bağlı.** [request_outbound_provisioning](/Users/onurmatik/Projects/OperationalInbox/inbox/services/jobs.py:237), receiving hazır değilken “Verify receiving with a real test email before enabling sending” hatası verdi. Send-only akışı bir backend capability değişikliğidir; yalnızca UI copy değişikliği değildir. Ownership ve sending DNS verification zorunlu kalmalı. Existing MX'e dokunmadan sending kurulabilmeli.
3. **Queued outbound conversation Inbox'ta görünmeyebilir.** Compose bir Conversation ve immutable revision oluşturuyor; provider handoff yapılmadan Message bulunmayabiliyor. [inbox_list](/Users/onurmatik/Projects/OperationalInbox/inbox/views.py:835) `message_count > 0` şartı kullanıyor. Birleşik history query'si OutboundMessage varlığını da dikkate almalı; pending veya failed ilk gönderimler kaybolmamalı.

## Model ve service temizliği

`inbox/models.py` içinde 33 concrete model bulunuyor. Custom OAuth tarafında ayrıca iki model var. Sayıyı azaltmak tek başına hedef değil; ürünün sorumluluğu olmayan workflow ve kullanılmayan state kaldırılmalı.

| Aday | Kod kanıtı | Önerilen işlem |
| --- | --- | --- |
| `Conversation.snoozed_until` | Runtime source'ta tanımı dışında kullanım yok. | Alanı kaldır. Snooze workflow ekleme. |
| `Conversation.merge_suggestion` | Ingestion subject benzerliği ile yazıyor; UI/API tüketmiyor. Thread merge yalnızca bu öneriyi taşıyor/temizliyor. | Alanı ve `threading.merge_suggestion()` heuristic'ini kaldır. RFC/Message-ID ile gerçek threading ve late-handoff merge korunur. |
| `ReplyDraft.rejected_at` | Runtime source'ta tanımı dışında kullanım yok. | Alanı kaldır. Revision, freshness ve approval invalidation korunur. |
| `ReportSchedule` | Yeni `create_domain()` artık oluşturmuyor. Frequency/aging alanlarının aktif scheduler consumer'ı yok. | Eski report workflow ile birlikte kaldır. |
| `Report`, `ReportItem` | Aktif UI/MCP akışı yok. Legacy REST report reads, retention ve thread-merge referansları var. | Yeni report üretimini kaldır; geçmiş veri ve legacy reads için compatibility planı tamamlandıktan sonra tabloları kaldır. |
| `Domain.timezone` | Runtime consumer yalnızca report service. | Report retirement ile birlikte kaldır. Display timezone domain'in mail altyapı state'i olmamalı. |
| In-app notification channel ve read state | Aktif producer yok. Legacy REST list/read endpoints var. | In-app kısmını retire et. `Notification` email security/domain/delivery alerts için kalır. |
| Server-side `ai.py`, `AgentRun`, `Classification` | Classification ve report job handlers var; aktif scheduler bu işleri üretmiyor. Classification için legacy REST override hâlâ var. | Agent-first yön için internal AI engine'i retire etmeyi öneriyorum. Mevcut classification data ve HTTP contract için kontrollü geçiş gerekir. |
| OpenAI SDK ve server model settings | Runtime usage internal AI service ile sınırlı. | Internal AI retirement tamamlanınca kaldır. MCP/OAuth ve OpenAI Apps challenge bundan bağımsızdır. |

`Classification` ve `AgentRun` yalnızca “import az” diye silinmemeli: public plan copy server-side classification vaat ediyor ve legacy API mevcut. Bu, ürün kapsamını sadeleştirme kararıdır. Önerilen hedefte agent kendi triage, summary ve reply metnini üretir; sunucu free-form tags, immutable drafts, mail ve delivery facts saklar.

Reports ve internal AI retirement tamamlanırsa beş model çıkarılabilir. Email alerts korunursa hedef core 28 inbox modeli olur. Geçişte mevcut veriye destructive migration uygulanmadan önce gerçek record counts, historical content ve pending DurableJob kinds incelenmeli. Legacy endpoints için bir compatibility window belirlenmeli; aynı MCP contract major içinde mevcut tool isimleri ve meanings değiştirilmemeli.

## Kalması gereken model grupları

| Grup | Modeller | Gerekçe |
| --- | --- | --- |
| Account ve auth | `User`, `EmailVerificationToken`, `SignupAttempt`, `APIToken`, `BillingProfile`, `StripeWebhookEvent` | Ownership, authentication, abuse controls ve billing idempotency. |
| Mail history | `Domain`, `Conversation`, `ConversationTag`, `Message`, `MessageRecipient`, `MessageReference`, `Attachment` | Adresleme, threading, archive/search ve attachment quarantine. |
| Domain altyapısı | `DomainDNSRecord`, `InboundRoute`, `InboundRoutingTransition`, `DomainTest` | Exact DNS instructions, forwarding/direct routing ve güvenli cutover. Bunlar default UI'dan gizlenebilir; veri lifecycle'ından çıkarılmamalı. |
| Gönderim | `ReplyDraft`, `ReplyDraftRevision`, `DraftApproval`, `OutboundMessage`, `DeliveryEvent`, `OutboundControl` | Agent'ın yazdığı exact revision, send authority, idempotency, delivery ve account pause. Compose snapshot'ı “draft UI yok” diye kaldırılmaz. |
| İşletim | `IngressEvent`, `DurableJob`, `RetentionPolicy`, `Notification`, `AuditEvent` | Ingestion dedupe, leases/recovery, retention, email alerts ve audit evidence. |
| OAuth | `OAuthApplication`, `OAuthRefreshFamily` | Native agent authorization, PKCE ve rotating refresh lifetime. |

## View ve template planı

- `dashboard()` ve `dashboard.html` retire edilir. `/app/` Conversations'a yönlenir. Domain readiness sorunları kısa bir notice olarak kalır.
- `inbox_list()` owner-wide Conversations query'sine dönüşür. Domain/recipient filters açık URL state olur. Aynı query Inbox, Sent ve Archive için kullanılır.
- `outbox()` ana page olmaktan çıkar. Delivery states ilgili timeline'da; account pause Settings'te. Eski `/app/outbox/` URL'si Sent görünümüne redirect olabilir. `OutboundMessage` ve delivery services korunur.
- `conversation_detail()` ve action views domain'i owned conversation'dan alır. Search/folder/domain return context kaybolmaz.
- `compose()` send service korunur. Form modal/drawer olarak progressive enhancement ile açılır; standalone route fallback olabilir. Send sonucu queued/delivered durumunu gerçek server state'ten gösterir.
- `agents()` geniş docs page yerine connection paneline dönüşür. API-token yönetimi aynı secondary Access girişinde bulunur. Detailed installation ve MCP docs ayrı kalır.
- `domain_detail.html` 789 satır içinde çok sayıda state taşıyor. İşlevler korunarak capability summary, next action, DNS details ve advanced routing partial'larına bölünür. Default ekran bir sonraki gerekli action'ı gösterir.
- `retention_settings()`, `api_tokens()`, `audit_log()` ve billing Settings'in bölümleri olur. Yan etkili POST endpoints ve CSRF koruması kalır.
- `navigation_context()` her page için tam mailbox recipient ağacı üretmeyi bırakır. Account navigation basit ve ucuz kalır.

Mevcut Django templates, Tailwind ve vanilla JavaScript yeterli. Bu değişiklik için yeni frontend framework veya yeni chat runtime önerilmiyor.

## Uygulama sırası ve acceptance

1. Bu görsel yön seçildikten sonra global Conversations, owned-record domain lookup, Sent/Archive filters ve inline delivery uygulanır. Multi-tab scoping ve queued-only conversation coverage eklenir.
2. Domain goal-first setup eklenir. Sending-only ownership + sending DNS checks ile çalışır; receiving test sadece receiving için istenir. Direct-MX ve forwarding regression tests korunur.
3. Legacy report/internal AI üretimi ve kullanılmayan fields temizlenir. Historical data + compatibility planı ile schema retirement tamamlanır. OpenAPI, public plan copy, technical docs ve fixtures birlikte güncellenir.
4. Domain UI, connection paneli ve Settings birleştirilir. Desktop ve mobile ana akışlar browser'da doğrulanır. Uygulama local `main` üzerinde kalır.

Verification sırasında `test_inbox_tracking`, `test_compose`, `test_mime_threading`, `test_mcp_server` ve `test_production_invariants` çalıştırıldı: **97 passed, 1 skipped**. Bunlar mevcut checkout'un baseline kontrolleridir; henüz yapılmamış redesign'ın test sonucu değildir. Provider DNS, gerçek mail delivery veya deployment doğrulanmadı.

Bu turda application source veya production schema değiştirilmedi. Audit evidence, task brief ve Superdesign draft hazırlandı. UI implementation için Superdesign workflow'ü görsel onay istiyor.

## Prototype verification

Kaydedilen version 9 browser'da 1440×900 ve 390×844 boyutlarında incelendi. Search ve explicit domain filter sonuçları daraltıyor; Sent ilgili outbound mail ve delivery state'i gösteriyor. Archive action ve Archive görünümünde mailin bulunması kontrol edildi. More, Starred ve Trash'i açıyor. Desktop list/detail, mobile list → thread → Back ve mobile Domains navigation çalışıyor. Mobile page width 390px ölçüldü; page-level horizontal overflow yok.

New email formunda `customer-care@acme.example` gibi farklı bir local part kullanılabildi. Prototype submit yalnızca “No real email was sent” notice'u verdi. Sending-ready olmayan example domain form tarafından kabul edilmez. Domain setup'ta Send email seçimi ownership + sending DNS gereksinimlerini gösterdi; receiving/MX/test adımı istemedi. Bu önerilen backend davranışıdır; mevcut application'da henüz uygulanmamıştır.

Agent setup gerçek public installation prompt'unu kullanır. Escape ile modal kapanması ve focus'un açan button'a dönmesi kontrol edildi. Form label'ları input'larla ilişkilendirildi. Tam accessibility veya provider verification yapılmış değildir. Settings alt bölümleri yerleşim ve scope örneğidir; gerçek account data veya credentials yüklemez.

## Önerilen ekranlar

![Conversations proposal](/Users/onurmatik/Projects/OperationalInbox/.superdesign/audit/2026-10-09/14-proposed-conversations.png)

![Sent ve inline delivery](/Users/onurmatik/Projects/OperationalInbox/.superdesign/audit/2026-10-09/15-proposed-sent.png)

![New email](/Users/onurmatik/Projects/OperationalInbox/.superdesign/audit/2026-10-09/16-proposed-compose.png)

![Secondary agent setup](/Users/onurmatik/Projects/OperationalInbox/.superdesign/audit/2026-10-09/17-proposed-agent.png)

![Domains proposal](/Users/onurmatik/Projects/OperationalInbox/.superdesign/audit/2026-10-09/18-proposed-domains.png)

![Goal-first domain setup](/Users/onurmatik/Projects/OperationalInbox/.superdesign/audit/2026-10-09/19-proposed-domain-goal.png)

![Mobile list](/Users/onurmatik/Projects/OperationalInbox/.superdesign/audit/2026-10-09/20-proposed-mobile-list.png)

![Mobile conversation](/Users/onurmatik/Projects/OperationalInbox/.superdesign/audit/2026-10-09/21-proposed-mobile-thread.png)

![Mobile Domains](/Users/onurmatik/Projects/OperationalInbox/.superdesign/audit/2026-10-09/22-proposed-mobile-domains.png)

## Screenshot evidence

### 1. Domain ekleme

![Domain başlangıcı](/Users/onurmatik/Projects/OperationalInbox/.superdesign/audit/2026-10-09/10-connect-domain.png)

### 2. Domain listesi ve DNS setup

![Domain listesi](/Users/onurmatik/Projects/OperationalInbox/.superdesign/audit/2026-10-09/06-domains.png)

![DNS setup](/Users/onurmatik/Projects/OperationalInbox/.superdesign/audit/2026-10-09/07-domain-setup.png)

### 3. Agent setup

![Agent setup](/Users/onurmatik/Projects/OperationalInbox/.superdesign/audit/2026-10-09/08-agent-setup.png)

### 4. Overview ve Inbox

![Overview](/Users/onurmatik/Projects/OperationalInbox/.superdesign/audit/2026-10-09/01-overview.png)

![Inbox](/Users/onurmatik/Projects/OperationalInbox/.superdesign/audit/2026-10-09/02-inbox.png)

### 5. Conversation okuma

![Conversation](/Users/onurmatik/Projects/OperationalInbox/.superdesign/audit/2026-10-09/03-conversation.png)

### 6. Mail gönderme ve delivery

![Compose](/Users/onurmatik/Projects/OperationalInbox/.superdesign/audit/2026-10-09/05-compose.png)

![Outbox](/Users/onurmatik/Projects/OperationalInbox/.superdesign/audit/2026-10-09/04-outbox.png)

### 7. Archive

![Archive](/Users/onurmatik/Projects/OperationalInbox/.superdesign/audit/2026-10-09/09-archive.png)

### Mobile verification

![Mobile Inbox](/Users/onurmatik/Projects/OperationalInbox/.superdesign/audit/2026-10-09/11-inbox-mobile.png)

![Mobile Outbox](/Users/onurmatik/Projects/OperationalInbox/.superdesign/audit/2026-10-09/12-outbox-mobile.png)

## Uygulanan değişiklikler

Conversations artık owner'ın tüm domain'lerini kapsayan default ekran. Inbox, Sent ve Archive aynı history üzerinde filter; Starred/Trash More altında. Session domain state kaldırıldı; conversation ve işlemlerinin domain'i owned record'dan bulunuyor. Queued-only outbound conversation'lar Sent'te görünür. Search ve domain filter URL'de açık kalır.

Overview, ayrı Outbox ve eski conversation-detail template'leri kaldırıldı. Eski Overview/Outbox URL'leri yeni workspace'e yönlenir. Compose ve exact draft review native dialog olarak açılır; standalone compose fallback kalır. Agent connection kısa bir prompt/dialog oldu. Settings account sending controls, Access, Retention, Audit ve Plan girişlerini toplar.

Domain setup Receive / Send / Both ile başlar. Send-only ownership ve sending DNS ile ilerler; MX veya receiving test gerektirmez. Sonradan receiving eklenebilir. Existing MX inspection, explicit route choice ve routing-transition checks receiving için korunur. Domain detail capability summary, next action, DNS ve advanced routing partial'larına bölündü.

`AgentRun`, `Classification`, `ReportSchedule`, `Report`, `ReportItem` ve internal AI/report services kaldırıldı. Inbox concrete model sayısı 33'ten 28'e düştü. Kullanılmayan timezone, snooze, merge suggestion, rejected timestamp ve in-app notification/read fields temizlendi; email alerts, retention, audit ve exact-revision send modelleri kaldı. OpenAI SDK ve server model settings kaldırıldı.

Migration 0015 eski kayıtları ID/timestamp/content ile AuditEvent snapshot'larına taşır; populated forward/reverse migration test'leri var. Legacy report/notification/classification REST contract'ları deprecated compatibility reads olarak korunur. Historical content retention kurallarına tabidir; normal audit API snapshot içeriğini açmaz. Retired job kinds yeni işler üretmez. MCP goal support, receiving-enable tool, OpenAPI, plugin/skill version ve public docs güncellendi.

Local main SQLite için korumalı backup alındı ve migration uygulandı. Migration öncesi main database'de domain/conversation/message/draft/outbound ve kaldırılan beş model için record count sıfırdı. Korunan count'lar değişmedi; integrity `ok`, foreign-key violation sıfır. QA örnekleri ayrı disposable database'de kaldı.

Full suite **417 passed, 1 skipped**, coverage **80.98%**. Son UI değişiklikleri için **72 passed**. Ruff, Mypy, Django check, migration drift, OpenAPI check ve Git whitespace check geçti. Desktop/mobile/tablet browser kontrolleri ve görsel karşılaştırma tamamlandı; [QA raporu](/Users/onurmatik/Projects/OperationalInbox/design-qa.md) sonucu `passed`.

Gerçek mail gönderilmedi, DNS değiştirilmedi ve production'a deploy yapılmadı. Coupled local QA preview tamamlanınca kapatıldı; port ve process cleanup doğrulandı.

![Uygulanan Conversations ekranı, örnek veri](/Users/onurmatik/Projects/OperationalInbox/.superdesign/implementation/2026-10-09/07-desktop-final.png)
