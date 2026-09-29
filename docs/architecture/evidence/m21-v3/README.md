# M21 v3 ölçüm kanıtları

Bu dizindeki özet ve sıkıştırılmış envanterler, 35 görülen geliştirme/regresyon
vakasının etiket sınırını ve karar otoritesini ayrı tutar. Envanter dosyaları
gzip sıkıştırılmış JSON'dur; hiçbir rakip örnekleme nedeniyle atılmaz.

- `measurement.json`: vaka bazında baseline/son snapshot ve aktif investigation
  sayıları; matching/nonmatching actor desteği; sorgu ve replay sonuçları.
- `baseline-rivals.json.gz`, `after-rivals.json.gz`: her kalan claim için
  actor/UID/episode/mechanism, admission, gerçek symptom yolları, support/witness,
  rakip kimlikleri, olası etki/tekrar temsili, gereken somut kanıt ve yetkili
  okumalar. `UNDETERMINED_UNLESS_POSITIVE_EXECUTION_PROOF` bağımsızlık iddiası
  değildir. Okuma yetkisi, eksik execution olgusunun kayıtta bulunduğu anlamına
  gelmez; `NOT_ESTABLISHED_BY_CURRENT_RECORD` kesin yokluk iddiası değildir.
- `observed-quota-example.json`: ölçümdeki pozitif etki açıklaması, kaynak claim'in
  başlangıç için temporal contradiction'ı ve belirleyici ham event kayıtları.
- `authority-examples.json`: pozitif tek-sorgulu RESOLVED geçişi ve aynı grafikte
  eksik rejection ile yetersiz kalan karşı örnek; seen35 skoruna dahil değildir.

Ham snapshot/active tanıları ve her bounded kaynak/backend çağrısının kaydedilmiş
cevabı, repo köküne göre `.local/causal-closure/release/` altında tutulur. Baseline
tam iddiaları `.local/causal-closure/baseline/` altındadır. Büyük read tape'ler bu
dizinde tekrar çoğaltılmaz. Bu teslimde bir SHA-256 manifest'i üretilmemiştir.

```sh
PYTHONPATH=. .venv/bin/python scripts/causal_closure_regression.py --out .local/causal-closure/recheck --workers 6
PYTHONPATH=. .venv/bin/python scripts/causal_closure_replay.py .local/causal-closure/recheck --workers 6
PYTHONPATH=. .venv/bin/python scripts/causal_rival_inventory.py .local/causal-closure/recheck --output .local/causal-closure/recheck-rivals.json.gz
PYTHONPATH=. .venv/bin/python scripts/causal_closure_summary.py --baseline .local/causal-closure/baseline --after .local/causal-closure/recheck --output .local/causal-closure/recheck-summary.json
```

Replay komutu snapshot kaynağı veya canlı backend oluşturmaz. Seed ve query
cevaplarını JSON bandından yeni nesnelere dönüştürür; yöntem, argüman, çağrı sırası,
bütün bandın tüketilmesi, nihai karar digest'i ve her sorgunun before/after
digest'i doğrulanır. Aynı snapshot'ı ikinci kez hesaplamak replay ölçümü değildir.
Snapshot'ın son kodla ayrıca doğrulanması ayrı bir kontroldür.

Baseline motor `2.0.0`, son motor `2.1.0` olduğundan eski ürüne ait provider replay
kontratını yeni motorla sessizce kabul etme yolu eklenmedi. Değerlendirme kaynak
bandı ve ürünün kalıcı replay kontratı ayrı mekanizmalardır.
