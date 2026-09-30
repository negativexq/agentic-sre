# M21 v3 — Nedensel açıklama ve karar kapanışı

Bu teslim actor-scoped claim/admission modelini korur; pozitif gözlem açıklaması,
frontier cevabı ve kapsamı sınırlı mekanizma teşhisi ekler. Engine `2.1.0`, karar
semantiği `m21.v3`, claim semantiği `m21.v2` ve D1
`m21.support.change-onset-path.v2`'dir. Incident recovery ayrı `NOT_ASSESSED` kalır.

## Ölçüm otoritesi

Baseline, mevcut `main` HEAD'i olan
`d9b7cf702dd7385dcd4e57700d391b292a30b008` üzerinde, uygulama değişikliklerinden
önce yeniden hesaplandı. Önceki rapordaki rebase öncesi sonuç kullanılmadı.
Snapshot sonucu **23 COMPETING_CAUSES, 12 INSUFFICIENT_EVIDENCE,
0 SUPPORTED_CAUSE**, doğru aktör etiketiyle eşleşen desteği bulunan **18/35**
vaka olarak doğrulandı. D1 dışında güçlü mekanizma desteği ve frontier cevabı
yoktu; wrapper'ın D1 RESOLVED sonucunu düşürmesi de kodda doğrulandı.

Eski kayıt aktif investigation ölçümü değildi. Bu çalışma ayrıca baseline
engine ile aynı 35 vakada bounded investigation çalıştırdı: **24 yetersiz,
10 rekabet, 1 tek olası neden**, **210 sorgu**, **47 karar değiştirici sorgu**.
Buradaki SUPPORTED_CAUSE, snapshot baseline'ın sıfır olmasıyla çelişmez:
girdi sınırı ve edinilen gözlemler farklıdır. Baseline kaynak bandı kaydedilmedi;
baseline için kaynak replay başarısı iddia edilmiyor.

35 vaka, daha önce görülen dev/regresyon setidir. Tahmin ve replay tamamlanmadan
ground truth yüklenmez. Üretim kurallarında vaka adı, benchmark etiketi veya
istenen sonuç filtresi yoktur. Sorgu bütçesi 6 tur/8 tool call, zaman sınırı
600 saniyedir; snapshot backend kullanılır, canlı provider/model maliyeti yoktur.

## Etiket eşleşebilirliği

Mevcut grader `_matches` değiştirilmedi. Object/event evreninde en az bir root
veya kabul edilen alias eşleşmesi bulunan **31/35** vaka scoreable'dır.

| Vaka | Eşleşmeyen etiket / görünen aktör |
|---|---|
| Scenario-29 | JVMChaos `.*adservice`; görünen adlar `otel-demo-ad-jvm-return-…` |
| Scenario-23 | Deployment `checkout-.*`; görünen aktör `checkout` |
| Scenario-38 | HPA `*.*`; geçersiz regex, grader literal fallback'i eşleşmiyor |
| Scenario-105 | Deployment `product-catalog-.*`; görünen aktör `product-catalog` |

Snapshot'ta eşleşen root temsili ve admission **29/35**, scoreable vakalar içinde
**29/31 (%93,5)**. Kalan iki scoreable temsil açığı ayrıca değerlendirilmelidir:
Scenario-1 etiketi Pod/Service ve alias düzeyindeyken motor Deployment/load-generator
temsil ediyor; Scenario-102 Namespace/otel-demo etiketi yerine kota/ReplicaSet
mekanizma aktörleri içeriyor. Bunlar otomatik olarak “motor gerçek mekanizmayı
kaçırdı” sayılmadı; etikete uydurmak için yeni alias eklenmedi.

Etiketle eşleşen olası neden desteği, doğru mekanizmanın bağımsız doğrulaması
değildir. Eşleşmeyen desteği de kanıtlanmış yanlış mekanizma diye saymıyoruz.
Vaka sayısı ve claim sayısı ayrı raporlanır; bir vaka iki kategoride de bulunabilir.

## Uygulanan karar sınırları

`CausalExplanation`, kaynak/hedef claim, aktör/UID, episode, yönlü mekanizma,
belirleyici observation/evidence kimlikleri, coverage, kural/sürüm, sonuç ve
kalan belirsizliği taşır. Aynı namespace, presentation group veya topology yolu
ilişki oluşturmaz. Paylaşılan evidence bağımsız doğrulama sayılmaz.

`m21.explanation.runtime-return.v1`, doğrulanmış endpoint/UID ve eşleşen
observation provenance ile belirli başarısız dönüşü açıklar. Bütün aktörü veya
uzaktaki hatanın başlangıcını açıklamaz. Mevcut propagation eliminasyonları ayrı
otoriteleriyle korunur; mekanizma bridge'i tek başına execution sayılmaz.

`m21.explanation.observed-quota-rejection.v1`, kotayı açıkça adlandıran ham
`Warning/FailedCreate` olayından belirli hedef instance'ın reddini açıklar.
Yalnız hedefin **bütün actor-local bulguları** bu gözlemlerden oluşuyorsa claim
rekabetten çıkar. Bağımsız initiating, başka hata veya zamanı belirsiz değişiklik
korunur. Kota ve failure evidence listelerindeki sunum amaçlı 3/4 kayıt kesintisi
kaldırıldı; tam gözlem kapsamı kontrol edilir. UID ve episode birleştirilmez.

Başlangıç için geç kalmış bir kota, sonraki reddin gözlenen aktörü olabilir.
`INITIATING_TIMING` çelişkisi korunurken bu sonraki rol açıklanabilir; açıklama D1
desteği üretmez. Diğer pozitif kaynak çelişkileri açıklama otoritesini engeller.
Döngüler yalnız observation kaydı kalır; claim elemesi, frontier kapanışı veya
bağımsızlık üretmez.

`m21.support.observed-quota-rejection.v1`, D1 desteğine ek olarak, origin zamanında
ham rejection ve **gerçek incident symptom'unun reddedilen subject olması**
koşuluyla `OBSERVED_MECHANISM_CAUSE` verir. Genel quota→service yolu yeterli
değildir. Tek uygun claim, bütün ilan edilmiş symptom kapsamının witness'larla
örtülmesi, açıklanmamış rakip ve açık materyal frontier olmaması halinde
`OBSERVED_MECHANISM_DISAMBIGUATED_V1` / `MECHANISM_VERIFIED_CAUSE` / `RESOLVED`
üretilir. Recovery sonucu üretilmez.

Frontier cevabı sorgu ilerlemesinden ayrıdır: pozitif rol bir claim'e aktarılabilir;
o claim'in adjudication'ı devam eder. Bütün bağlı sorular cevaplanmadıysa frontier
açık kalır, kısmi cevabın kanıtı korunur. NO_DATA, provider hatası, PROMOTED veya
bütçe bitmesi kapanış değildir. İlgili karşı kanıt/kanıt kaldırılması cevabı
geri çeker; ilgisiz context değiştirmez. Cevaplanmış sorular query kuyruğundan
çıkar; fiziksel sorgu kimliği sonuçsuz okumaların tekrarını önler.

Güçlü kural bu teslimde gözlenen quota admission reddiyle sınırlıdır. Fault
execution, config consumption ve diğer mekanizmalar için gereken instance/zaman
bağı kanıtlanmadığında açık kalır. Bu sınır, kaynakların bu kanıtı hiçbir zaman
sağlayamayacağı iddiası değildir.

## Test otoritesi

`proof.py` T4 ve ablation T5/T6'da kullanılan `has_supported_cause()` geçişi
incelendi. v2/v3 için hedef `m21.unique-possible-cause.v1`; eski
`legacy.resolved.v1` ile aynı başarı olarak raporlanmıyor. Artifact ayrı ayrı
possible-cause disambiguation, observed mechanism, positive rival elimination,
strong diagnosis ve recovery alanlarını taşır. Güçlü kural ablation'ı,
açıklamayı ve olası desteği korurken kesin teşhisi geri çekiyor.

| Zorunlu davranış | Test kanıtı |
|---|---|
| Neden ve açıklanan etki ayrımı | `test_observed_rejection_explains_effect_and_authorizes_scoped_resolution` |
| Bağımsız ikinci initiating korunur | `test_independent_initiator_on_target_survives_explanation` |
| Yalnız topology yetmez | `test_identical_graph_without_recorded_execution_is_only_possible` |
| Belirleyici kanıt kaldırılınca yetki çekilir | `test_ablation_uid_episode_and_unrelated_context`, execution-rule ablation |
| NO_DATA/provider hatası kapatmaz | `test_no_data_and_provider_errors_leave_material_frontier_open` |
| Pozitif cevap claim'e aktarılır | `test_frontier_positive_answer_transfer_and_reopening` |
| Döngü ve evidence tekrarı kesinlik üretmez | cycle ve duplicate-evidence testleri |
| İlgisiz context sonucu değiştirmez | `test_answered_question_is_stable_under_unrelated_context` |
| UID/episode ayrımı korunur | UID/episode ablation; önceki actor-locality/admission testleri |
| Pozitif örnek kesin teşhise ulaşır | gerçek normalizer/selector ile `test_new_recorded_rejection_changes_decision_and_replays` |
| Benzer eksik kanıtlı örnek ulaşmaz | `test_same_graph_empty_reads_never_authorize_resolution` |
| Kaydedilmiş kaynak aynı geçişleri üretir | aynı integration testi ve seen35 serialized read tape replay |

Ek testler geç rol/başlangıç çelişkisi ayrımını, kısmi frontier cevabını,
farklı gerçek mekanizmaları, non-rejection event'lerin yetkisizliğini ve çıplak
`memory` quota deklarasyonunun yalnız soru oluşturmasını kapsar.

## Son ölçüm ve envanter

[Vaka bazlı ölçüm](evidence/m21-v3/measurement.json) ve
[tam rakip envanterleri](evidence/m21-v3/README.md) teslimin parçasıdır.

| Ölçüm | Baseline snapshot | Son snapshot | Baseline aktif | Son aktif |
|---|---:|---:|---:|---:|
| Scoreable vaka (tam kayıt etiket eşleşmesi) | 31/35 | 31/35 | 31/35 | 31/35 |
| Eşleşen aktör temsili / admission | 29 / 29 | 29 / 29 | 17 / 17 | 17 / 17 |
| Eşleşen olası destek: vaka / claim | 18 / 53 | 18 / 53 | 8 / 8 | 8 / 8 |
| Eşleşmeyen olası destek: vaka / claim | 13 / 24 | 13 / 24 | 3 / 7 | 3 / 7 |
| Eşleşen / eşleşmeyen güçlü destek | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 |
| Tek olası neden: SUPPORTED_CAUSE | 0 | 0 | 1 | 1 |
| COMPETING_CAUSES | 23 | 23 | 10 | 10 |
| INSUFFICIENT_EVIDENCE | 12 | 12 | 24 | 24 |
| Kesin teşhis: MECHANISM_VERIFIED_CAUSE / RESOLVED | 0 | 0 | 0 | 0 |
| Pozitif açıklanan claim | 0 | 1 | 0 | 0 |
| Kalan unresolved claim | 135 | 134 | 78 | 78 |
| Bağımsızlığı güçlü kuralla kurulmuş çoklu neden | 0 | 0 | 0 | 0 |
| Tam cevaplanan materyal frontier | 0 | 0 | 0 | 0 |
| Kısmi pozitif cevap taşıyan frontier | 0 | 1 | 0 | 0 |
| Pozitif cevaplanan claim-rol sorusu | 0 | 1 | 0 | 0 |
| Açık materyal frontier | 636 | 637 | 481 | 481 |
| Recovery assessed | 0 | 0 | 0 | 0 |

Açık frontier sayısındaki +1, pod admission için geçerli çıplak `memory` quota
beyanının da soru oluşturmasıdır. Mevcut frontier'lar ilişkisiz context nedeniyle
yeniden açılmadı. Bu yeni sorunun bir hedef rolü cevaplanıyor; diğer bağlı
claim'lerin rolleri bilinmediğinden soru tümden kapanmıyor. Bağımsızlığı kurulmuş
neden sayısının sıfır olması, kalan rakiplerin bağımsız olmadığını kanıtlamaz.

Baseline kalan envanteri **212 claim** içerir: **77 desteklenen**, **135 unresolved**.
Son envanter **211 claim**: destek sayısı değişmedi, bir unresolved etki açıklandı.
Unresolved bulgu türleri başlangıçta FAULT_INJECTION 60, FAILURE_EVENT 37,
ROLLOUT_RESTART 14, RESOURCE_PRESSURE 8, DEPENDENCY_ERRORS 7, CONTAINER_FAILURE 6,
CONFIG_CHANGE 5, SPEC_CHANGE 1 ve IMAGE_CHANGE 1 idi. Bir claim birden çok tür
barındırabildiğinden bunlar toplanıp claim sayısı olarak kullanılmaz. Sonuçta
FAILURE_EVENT bir azalır; sekiz unresolved aynı-aktör/episode temsilinin
birleştirilebilirliği kanıtlanmadığından otomatik birleştirme yapılmadı.

Gerekli ayrımlar envanterde bulgu bazında somuttur: fault için exact target UID ve
uygulama interval'iyle execution; config için revision-bound consumption;
restart/image için revision ve başlatan işlem; dependency error için eşlenmiş
runtime return; pressure için aynı instance/zamandaki demand/enforcement;
failure için açıkça atfedilmiş rejection veya başka yerel initiating kanıtı.
`authorized_reads` hangi sorgunun yapılabildiğini gösterir; bu, gereken execution
olgusunun kayıtta bulunduğunun kanıtı değildir. Erişilebilir ama kanıtlamayan veri,
okuma yetkisi olmayan soru ve kanıtın bulunmaması ayrı tutulur.

[Gerçek pozitif açıklama](evidence/m21-v3/observed-quota-example.json),
Scenario-102'de `ResourceQuota/otel-demo-memory` →
`ReplicaSet/ad-554b849958` ilişkisidir; hedef UID
`8a217393-53d2-4d89-9ea3-5d6e9f42929e` korunur. Incident onset
15:31:21 UTC, ilk kayıtlı rejection 17:54:41 UTC'dir. Kota için başlangıç
çelişkisi korunur. `k8s_events_raw.tsv:764` ile başlayıp `:810` ile biten
**17 kayıt kimliği**, aynı rejection serisinin kapsamını taşır; 17 bağımsız
nedensel doğrulama sayılmaz. Bütün hedef bulguları bu seri olduğundan etki
claim'i açıklanır. Namespace ground truth bu kota mekanizmasının doğru/yanlış
olduğunu ayrıca etiketlemediği için sonuç aktör skoru artışı gibi sunulmaz.

| Investigation ölçümü | Baseline | Son |
|---|---:|---:|
| Fiziksel sorgu | 210 | 210 |
| Karar değiştiren sorgu, ortak eski projection | 47 | 47 |
| Karar değiştiren sorgu, yeni causal digest dahil | ölçülmedi | 47 |
| Discriminator taşıyan sorgu | 210 | 210 |
| Tekrarlanan aynı fiziksel sorgu | 0 | 0 |
| NO_DATA | 115 | 115 |
| Tur bütçesinde duran vaka | 35 | 35 |
| Kaydedilmiş kaynak final/transition replay | kaydedilmedi | 35/35 |

Son aktif ölçümde **481 materyal soru** `BLOCKED_BUDGET` olarak açık kalır.
Altı turluk bounded investigation, tam snapshot'taki geç quota rejection'ını
bu vakada edinmedi; aktif sonuçta yeni explanation/closure yoktur. Full-source
kazanımı aktif döngü kazanımı olarak saymıyoruz. Sorgu otoritesi mevcut açık
sorulara bağlıdır; bütçenin bitmesi tamamlanma değildir. Snapshot backend'inde
canlı provider/model çağrısı yoktur; canlı ortam maliyet/başarı ölçümü yapılmadı.

[Pozitif ve benzer negatif authority örnekleri](evidence/m21-v3/authority-examples.json)
gerçek normalizer ve deterministic selector üzerinden çalışır. Pozitif
StatefulSet admission örneğinde başlangıç `INSUFFICIENT_EVIDENCE`, bir
`incident_events` sorgusunun yeni rejection kanıtından sonra
`MECHANISM_VERIFIED_CAUSE / RESOLVED` olur; frontier claim'e aktarılır ve recovery
`NOT_ASSESSED` kalır. Aynı yapıda rejection bulunmayan örnek üç okumadan sonra
`INSUFFICIENT_EVIDENCE` kalır. İkisi de serialized kaynak bandıyla aynı geçişleri
üretir. Bunlar davranış otoritesi testleridir, 35 vaka başarı oranına eklenmez.

**35 vakada kesin teşhis başarısı artmadı.** Teslimin doğrulanmış katkısı çalışan
explanation/frontier/strong-decision yolu, bir gerçek kayıtta kanıtlı etki ayrımı
ve pozitif/negatif karar geçişleridir. Sıfır yanlış RESOLVED, tek başına başarı
olarak kullanılmadı. Daha geniş mekanizmalar ve mevcut bütçede edinilemeyen
kanıtlar açık sınır olarak kalır.

## Doğrulama ve yeniden üretim

- Tam test paketi: **2170 passed, 23 skipped**; bir Starlette bağımlılık
  deprecation uyarısı. Önceki actor-locality/admission testleri korunur.
- `mypy apps packages tests scripts`: **360 dosya**, başarılı.
- Ruff lint/format, `git diff --check` ve web `npm run build`: başarılı.
- Son kodda full snapshot tanılarının tamamı ayrıca yeniden hesaplanıp kaydedilen
  tanının bütün alanlarıyla karşılaştırılır; kaynak replay bundan ayrı yapılır.
- Bağımsız replay komutu yalnız serialized seed/backend cevaplarına erişir;
  snapshot kaynağı/provider oluşturmaz, bütün çağrı bandını tüketir, final ve
  tüm before/after karar digest'lerini karşılaştırır.

[Artifact indeksi ve komutlar](evidence/m21-v3/README.md) ile birlikte saklanır;
artifact'ların SHA-256 manifest'i bu teslimde üretilmemiştir.
Son üretim kodu için doğrulama hash'i, ilk ölçüm hash'inden ayrı kaydedilir:
son scope-sunum koruması karma güçlü/D1 destek grubunu topluca yükseltmez.
35 snapshot'taki bütün alan eşitliği ve aktif bantların son kodda replay'i bu
son düzeltmenin ölçülen sonuçları değiştirmediğini ayrıca kontrol eder.

## Denetleyici-spawn açıklaması, ölçüm (2026-09-30)

`m21.explanation.controller-spawn.v1` (35 görülen dev vakası; tam regresyon, aktif investigation
dahil, ardından kayıtlı replay). Kuraldan önceki koşuyla karşılaştırma:

| Ölçüm | Önce | Sonra |
|---|---:|---:|
| Kayıtlı replay (kaynak ve transition eşitliği) | 35/35 | 35/35 |
| Snapshot tanısı | 23 COMPETING, 12 INSUFFICIENT | 20 COMPETING, 12 INSUFFICIENT, 3 SUPPORTED_CAUSE (19, 29, 91) |
| Aktif son tanı, sorgu, karar değiştiren sorgu | 10/24/1, 210, 49 | aynı |
| Açık materyal frontier, cevaplanan frontier | 637, 0 | 637, 0 |
| Açıklanan claim | 1 | 104 |
| Unresolved claim | 150 | 96 |
| Güçlü destek | 0 | 0 |

Üç yeni `SUPPORTED_CAUSE`'ın üçünde de tanı durumu onset kümesi boyunca kararsızdır
(`TIMING_SENSITIVE`); §10.2 notlu katman kuralı gereği düşürülmemiştir, ama kesinlik artışı olarak
sunulmamalıdır.

**Etiket eşleşmeli destek 56 → 14** (11 vakada değişti). Bu, etiketlerin deney nesnelerini adlandırdığı
vakalarda desteklenen tek ailenin artık Schedule olmasından gelir. Etiket varlık kimliğini skorlar,
mekanizma doğruluğunu değil; kural etikete göre ayarlanmadı ve bu düşüş bir doğruluk hükmü değildir.
Hangi varlığın raporlanacak kök neden olduğu (yinelenen fault'ın tanımı olan Schedule mı, tek bir
yürütme mi) ayrı bir modelleme sorusudur ve etiket eşleşmesiyle karara bağlanmamalıdır.

## Fault-execution desteği: ölçüm (2026-09-30)

`m21.support.observed-fault-execution.v1`, 35 gerçek yakalamada (spawn-full ile karşılaştırma,
`PYTHONHASHSEED=0`): tanı durumları, claim seviyeleri ve state 35/35 vakada aynıdır (20 COMPETING /
12 INSUFFICIENT / 3 SUPPORTED); replay 35/35 PASS. Kural 288 claim'den ikisinde tetiklenir
(Scenario-18, Scenario-22; ikisinde de taşıyıcı Schedule instance'ı, tanık hedef pod'un kendisidir).
İkisinde de desteklenen aile birden fazla olduğundan `OBSERVED_MECHANISM_CAUSE` seviyesi ve
`RESOLVED` oluşmaz; zaman kararlılığı STABLE. Hiçbir vakada yetki geri çekilmedi (STRONG_MECHANISM
withheld = 0). Yanlış `RESOLVED` = 0.

Kapsam sınırı ölçümle örtüşür: hedef pod'da aralık içi etki gözlemi 35 vakada iki claim'de vardır;
hizmet düzeyi semptomlar örtülmediği için bu kuralla `RESOLVED` erişilebilir değildir. Bu, kuralın
başarısızlığı değil, yol kanıtı eksikliğinin ölçümüdür.

### Fault hedefinden semptoma yol kapsamı (2026-09-30, salt-okunur)

12 chaos vakasında 473 hedef: hedef servisi semptom adları arasında 293, semptom servisinden
hedefe gözlenmiş çağrı yolu 348 (hep 1 sıçrama), hedeften semptoma eşleşmiş hata yayılımı 29
(hepsi Scenario-19), yayılım hem `Applied` sonrası başlayıp hem aralıkla çakışan 1. Çağrı yolu
etki kanıtı sayılmaz (hedeflerin %74'ünde bulunur). Servis düzeyi etki ilişkisi sözleşmede
tanımlandı, uygulaması own-testbed ölçümüne ertelendi; seen-35'te tek bir hedefe bakarak
parametre seçmek kuralı o hedefe uydurmak olurdu.
