# Aktöre özgü nedensel iddialar — uygulama ve doğrulama

Tarih: 2026-09-29. Başlangıç HEAD: `89112934584e97729d5ed27fba5a61bb5a68d057`.
Motor geçişi: `1.3.0` → `2.0.0`; karar sözleşmesi: `m21.v2`; rapor: `2.2`.
Yerel uygulama; merge veya deployment yapılmadı.

## 1. Doğrudan yeniden üretilen sorunlar

Başlangıç kodunda bağımsız küçük girdilerle şu davranışlar doğrulandı:

- Başka grup üyesinin initiating bulgusu seçilen aktöre `SUPPORTED` sağlıyordu.
- `linked_symptoms` boşken actor→member `PATH` desteğe yetiyordu.
- Bağlantısız boş hipotez eklemek `RESOLVED` sonucunu `AMBIGUOUS` yapıyordu.
- Aynı bileşendeki iki başlatıcıdan yalnız biri seçiliyordu. Puanları `(1,10)`
  ve `(100,10)` yapmak aktörü değiştiriyor; iki başlatıcı tek iddiada birleşiyordu.

Başlangıçtaki resolver/formation hedefli testleri: 46 geçti. Bu sonuç mevcut
hataların eski testlerle korunabildiğini de gösterdi.

## 2. Çalışan karar modeli

`Hypothesis` genişletildi; ayrı bir paralel RCA modeli eklenmedi. Sunum grubu
altındaki her aktör, UID ve incident-onset bölümü ayrı iddia olarak korunuyor.
Puan grup sunumunda kullanılabiliyor, iddianın kanıt sahipliğini belirlemiyor.
Farklı UID'ler arasındaki nesne sürümleri content diff olarak yorumlanmıyor.

Ortak `claims.py` actor/instance/episode bulgularını ve incident semptomuna
kesintisiz yönlü ilişkiyi doğruluyor. Incident'e bağlı hata gözlemi admission
alabilir; initiating kanıtı olmadığı için support alamaz. Bağlantısız gözlem
context olarak korunur, yeni kanıtla aday olabilir. Member yolları ayrıdır.

D1 v2, aktöre ait başlangıç bulgusu, zaman, gerçek semptom, ilişki zinciri,
kaynak kimlikleri, instance, coverage/eksikler ve kural sürümünü içeren
`CausalWitness` üretir. Destek düzeyi `POSSIBLE_INITIATING_CAUSE` olur.
Yapısal yol mekanizmanın gerçekleşmesini ispatlamaz. Doğrulanmış runtime
propagation admission'a katkı sağlar; failure origin tek başına initiating
support değildir. Pod attribution exact UID gerektirir.

Resolver admitted iddiaları karşılaştırır; pozitif çelişki/propagated-effect
kurallarını korur. Finding türü sayısına dayanan dominance otoritesi kaldırıldı.
Bağımsız gerçek rakipler kalır. `SUPPORTED_CAUSE` tek desteklenen olası nedeni,
`COMPETING_CAUSES` gerçek rekabeti, `INSUFFICIENT_EVIDENCE` destek eksikliğini
bildirir. D1 tek başına eski `RESOLVED` yetkisini vermez.

Mevcut structural frontier, tüm snapshot'larda hesaplanır ve ilgili iddia/yol
üzerindeki upstream mekanizmalara bağlanır. Planner admitted iddiaları ve bu
bağları kullanır. NO_DATA, provider hatası, bütçe bitmesi ve tamamlanan okuma
frontier kapanışı sayılmaz. Genel mekanizma kapanış kuralı eklenmedi.

## 3. Sözleşme değişiklikleri

[M21 sözleşmesi](m21-causal-semantics-contract.md) ve
[supported-leader sözleşmesi](m21-supported-leader-contract.md) güncellendi.

Korunan ilke: **Eksik kanıt çelişki veya nedensellik yokluğu kanıtı değildir.**
Değiştirilen çıkarım: **Her gözlem pozitif elenene kadar root rakibidir.**
Admission artık pozitif incident ilişkisi ister. Topic A'nın covered-no-path
şartları pozitif noncausality iddiası için korunur; context sınıflaması bu
iddiayı taşımaz. D1 audit-only olmaktan sürümlü witness kuralına geçti.
Uygulanmamış supported-leader önerisi eski support üzerine eklenmedi.

Geliştirme sırasında D1'e `RESOLVED` yetkisi veren ara ölçüm iki yanlış kesin
sonuç üretti. Bu sonuç kabul edilmedi. Nihai kural tüm vakalarda aynı şekilde
possible-cause kapsamını korur; senaryo/isim/ground-truth filtresi kullanılmaz.

## 4. Gerçekten çalıştırılan doğrulama

- Tam pytest: **2109 geçti, 23 atlandı**, hata yok (47,85 saniye).
  Tek uyarı Starlette/AnyIO deprecated alias uyarısıdır.
- Mypy: **348 kaynak dosyasında hata yok**. Ruff lint/format ve
  `git diff --check` geçti. Frontend `npm run build`, TypeScript
  derlemesiyle birlikte geçti.
- `test_causal_claims.py` içindeki davranış testleri 16 kabul başlığını kapsar:
  context invariance/promotion, locality/ablation, symptom anchoring,
  score/rename invariance, iki initiator, gerçek rakip, unknown dependency,
  failure-origin sınırı, pozitif çelişki, UID/dönem, serialized parity,
  sekizden fazla kayıt ve budget/provider honesty.
- Ek UID lifecycle testi, aynı isimli iki UID arasında sahte config change
  oluşmadığını; aynı UID'de gerçek diff'in korunduğunu doğrular.
- Ürün trace/log/traffic entegrasyonları, exact-instance otoritesi, nötr okuma,
  recorded-read replay ve support/rule ablation kontrolleri çalıştırıldı.
- Eski testlerdeki member attribution, skorla tek aktör ve dominance beklentileri
  yeni davranışa taşındı. Geç HPA bulgusunun pozitif elenmesi ayrıca doğrulandı;
  elenmiş HPA artık seçilmiş aktör olarak gösterilmiyor.

## 5. Önce/sonra: erişilebilir 35 vaka

Bu veri daha önce görülmüş geliştirme/regresyon setidir; kör başarı ölçümü
değildir. Snapshot tahmini bittikten sonra ground truth yalnız ölçüm için açılır.
[Önce](evidence/m21-v2/before.json) ve [sonra](evidence/m21-v2/after.json)
tam vaka envanterleridir; sekiz kayıt kırpması kullanılmaz.

| Ölçüm | Önce | Sonra |
|---|---:|---:|
| Gerçek aktör en az bir iddia olarak korunuyor (vaka) | 27/35 | 29/35 |
| Gerçek aktör en az bir admitted iddiada (vaka) | 27/35* | 29/35 |
| Korunan gerçek aktörün tüm iddiaları admission dışında (vaka) | 0* | 0 |
| Ground truth aktörü hiçbir iddiada yok (vaka) | 8 | 6 |
| Ground truth ile eşleşmeyen destek iddiası | 39 | 24 |
| En az bir eşleşmeyen destek içeren vaka | 30 | 13 |
| Ground truth ile eşleşen destek içeren vaka | 18 | 18 |
| Ground truth ile eşleşen destek iddiası | 18 | 53** |
| Yanlış `RESOLVED` | 0 | 0 |
| `RESOLVED` | 0 | 0 |
| `AMBIGUOUS` | 30 | 23 |
| `INSUFFICIENT_EVIDENCE` | 5 | 12 |
| Materyal frontier ile sınırlanan vaka | ölçülmüyordu | 23 |
| Context olarak saklanan iddia | 0 | 770 |
| Unresolved iddia | 326 | 135 |
| Aynı case yeniden hesaplandığında digest eşitliği | ölçülmedi | 35/35 |

\* Eski modelde varsayılan rekabet evreni; yeni positive admission ile eşdeğer
bir kalite ölçümü değildir.

\** Bir aktörün farklı instance/dönem iddiaları ayrı tutulduğu için 53 sayısı
53 doğru teşhis anlamına gelmez. Doğru aktöre destek bulunan vaka sayısı **18'de
kaldı**. Ground-truth eşleştirmesi aktör düzeyindedir; her instance ve mekanizma
iddiasının bağımsız doğruluğunu doğrulamaz. Eşleşmeyen destekler de bu sınırlı
etiket setine göre sayılmıştır.

Sonuçların 23'ü `COMPETING_CAUSES`, 12'si `INSUFFICIENT_EVIDENCE`.
23 vakada destek yanında unresolved admitted rakip de vardır. Hiçbir vaka
tek `SUPPORTED_CAUSE` teşhisine ulaşmadı. Context kaynaklı gereksiz blokajın
sayısal baseline karşılaştırması yapılmadı: tek trace bunu ölçemez; ilgili
invariance ve planner davranışı testlerle doğrulandı. Ölçüm alanı bu nedenle
`null` bırakıldı.

Gerçek aktörün üretilemediği altı vaka: Scenario-1, Scenario-102, Scenario-105,
Scenario-23, Scenario-29, Scenario-38. Bu açık sınırlama isim bazlı kurallarla
kapatılmadı.

Yeniden çalıştırma:

```sh
PYTHONPATH=. .venv/bin/python scripts/causal_claim_regression.py --output .local/causal-claims/recheck.json
.venv/bin/pytest -q
.venv/bin/mypy apps packages tests scripts
.venv/bin/ruff check apps packages tests scripts
cd apps/web
npm run build
```

## 6. Doğrulanamayanlar ve sınırlar

Snapshot karşılaştırması aktif investigation loop'u çalıştırmaz; gerçek provider
okuma sayısı/maliyeti ve tasarruf iddiası yoktur. Bunlar JSON'da `null`.
Yeni kör veri, canlı production ve hizmet iyileşmesi ölçülmedi. Frontend build
başarılı; bu turda görsel tarayıcı QA yapılmadı. Ortam gerektiren atlanmış
23 PostgreSQL testi `TEST_POSTGRES_URL` tanımlı olmadığı için atlandı.

Yanlış/eşleşmeyen support sıfırlanmadı. Admission ve witness sahipliği
düzeltildi; D1'in olası-neden kapsamı korundu. Genel execution-proof veya
mekanizmaya özgü frontier closure eklenmedi. 35/35 digest tekrarı aynı case'in
yeniden hesaplanmasıdır; bunun recorded-source replay ile aynı ölçüm olduğu
iddia edilmez. Recorded replay ayrı entegrasyon testleriyle doğrulandı.

## 7. API ve kayıt/replay geçişi

API/rapor/UI `decision_semantics`, `diagnosis_status`, `claim_level`, context ve
material frontier alanlarını taşır. Investigation ayrı, `incident_recovery`
`NOT_ASSESSED` olarak ayrıdır. Legacy `root_cause` seçilen aktör alanıdır;
kapsam/statü okunmadan kesin neden kabul edilmemelidir. Eski `RESOLVED`
tüketicilerine daha zayıf D1 kanıtıyla yeni kesin sonuç verilmez.

Eski belgeler legacy varsayılanlarıyla okunabilir. Eski hipotezler sessizce v2
admission almaz. Engine-version replay denetimi eski motor koşusunu açıkça
unsupported sayar; yeniden değerlendirme yeni sürümlü koşu gerektirir.
Admission, witness ve kararı etkileyen frontier bağları canonical digest'e
girer; nötr sorgu yaşam döngüsü ilerlemesi nedensel kanıt sayılmaz.

## Push öncesi güncel main ile doğrulama

Uzak `main` üzerindeki `889c40a` commit'ine rebase edildi. Yeni channel/K coverage
kodları ve audit alanları korundu; üç dosyadaki çakışma iki değişiklik birlikte
kalacak şekilde çözüldü. Birleşik kodda **2150 test geçti, 23 PostgreSQL testi
atlandı**; mypy **352 dosyada** başarılı, Ruff lint/format başarılı.
Yukarıdaki 35 vaka ölçümü rebase öncesi koşudur; rebase sonrası yeniden
benchmark çalıştırıldığı iddia edilmez. Gelen coverage ekleri audit-only'dir.
