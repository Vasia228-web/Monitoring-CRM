"""Райони й ЖК (Блок 4, крок E10, D57).

Один довідник у config/places/*.toml (райони, села громади, орієнтири; ЖК з
написаннями, id DOM.RIA і районом; правила й пороги) і похідні ключі оголошень:

  * listings.district_key / complex_key (+ «як визначено»: district_how,
    complex_how) — заповнює крок циклу «райони й ЖК» (`cli.py places assign`) ЛИШЕ
    там, де порожньо або «не визначено»; зміна вже визначеного ключа лише
    рахується (would_change) і видна на /status, але не застосовується;
  * properties.district_key / complex_key / place_area / place_conflict — значення
    квартири (кеш, який зведення будує щоразу, як і решту полів квартири);
  * listings.row_district / row_complex / row_area — значення квартири на кожному її
    оголошенні: за ними фільтр «Район»/«ЖК» і лічильники списку беруться з
    покривного індексу (Блок 2), без з'єднання з properties. Пише їх ОДНА функція —
    `dedup._sync_rows`.

Сирі поля (district, complex_name, location, title) ніхто не змінює.
"""
