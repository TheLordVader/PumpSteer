# PumpSteer 3.0 med passiv Heat Pump Pilot

Pilot lär sig hur innetemperaturen reagerar under vanlig drift. Base beräknar
och skickar styrvärden precis som i referensen `81632a4`. I demon är Thermal
Intelligence passiv och adaptiv bromsning avstängd. Kandidaten bygger på
`47d6b77`, där den passiva kedjan, ForecastAudit och Store-rättningen ingår.

## Från värmesignal till jämförbar prognos

- **Till/från.** En verifierad signal för aktiv rumsvärme ger drifttidsandelen
  över ett intervall mellan två inomhusobservationer.
- **Inverter.** En verifierad rumsvärmeintensitet på 0–100 % med källtidsstämpel
  ger en tidsvägd andel på samma skala. Kontinuerlig drift kan då bidra genom
  att belastningen varierar.
- **Datakvalitet.** För gamla värden, motsägande driftläge och avbrott kan göra
  ett intervall oanvändbart. Varmvatten, avfrostning och okänd drift hanteras
  som otillgängliga när värmekällan rapporterar dem. En kvalificerad HA-signal
  räcker oavsett tillverkare. [Inläsning och kontroll](../custom_components/pumpsteer/heat_operation.py)
- **Prognos.** Husmodellen tränas på avslutade intervall. Prognosen fryser
  utetemperatur, modellparametrar och den senast observerade värmeandelen.
  ForecastAudit jämför med oförändrad innetemperatur vid samma senare godkända
  observation. Horisonterna är 30, 60 och 120 minuter. [ForecastAudit](../custom_components/pumpsteer/pilot_forecast_audit.py)

Den rapporterade värmeandelen beskriver drift eller belastning. En separat
värmemätning behövs för att uttrycka levererad värme i kW. Förutsättningarna för
att lära sig husets respons är minst 24 kvalificerade träningstimmar och
tillräcklig variation. Base kan användas utan värmetelemetri.

## Kör demonstrationen lokalt

Testerna använder syntetiska givare och mottagare. De körs från kandidatens
arbetskopia i en separat Python 3.14.10-miljö och behöver ingen HA-server.

```sh
python -m pip install 'homeassistant==2026.11.2' 'pytest==9.2.0' \
  'pytest-asyncio==1.4.1.post1' 'numpy==2.5.4'
python -B -m pytest -vv -o asyncio_default_fixture_loop_scope=function \
  tests/test_passive_pilot_house.py \
  tests/test_pilot_response_evidence.py \
  tests/test_thermal_passive_faults.py
python -B tests/probes/check_configure_native_ha.py
python -B -m pytest -q -o asyncio_default_fixture_loop_scope=function
```

| Prov | Vad som verifieras |
|---|---|
| `on-off` och `inverter` | I vardera fallet används 25 syntetiska träningstimmar och en senare observation bildar ett prognospar. Inverterfallet växlar mellan 20 och 80 % utan att stanna. |
| Kontinuerlig binär värmesignal | För liten variation ger `gain_frozen`. |
| Asynkrona rapporter | Ändringar mellan kontrollcykler bevaras. Gamla eller återpublicerade mätningar och luckor ger ingen falsk värmehistorik. |
| Base och Store | Identiska styrvärden och tjänsteanrop med och utan passiv TI. Långsam läsning, återförsök eller avslutande lagring håller endast tillbaka den passiva kedjan. |

128 riktade tester och hela sviten på 1 741 testfall passerade. I ett separat
syntetiskt Core-prov gav åtta kontrollcykler samma PI-, broms- och SAFE-resultat
och samma 16 Ohmigo- och GOS-anrop som `81632a4`.

## Vad som återstår att belägga

Verklig drift har gett observationer av driftläge och belastning. Det befintliga
fältunderlaget saknar kvalificerade Pilot-intervall och oberoende prognospar för
den här modellen. Prognosnytta mot referensen återstår att visa. ForecastAudit
bedömer accepterade temperaturändringar. Resultatet beskriver den täckta delen
av driften. Konfigurerad Generic Output kan styra som vanligt, medan husmodellens
insamling och prognos väntar i den konfigurationen.

I Store-provet kunde kandidaten läsa gemensam historik och ett äldre prognospar.
`81632a4` kunde läsa den gemensamma historiken ur kandidatens Store. Den nya
Pilot-kurvan kräver kandidatkoden, så spara kopior av Store från båda revisionerna
vid en eventuell återgång.

## Kopplingen till Johans v2.3.0

Båda använder HA-domänen `pumpsteer`. En gemensam version behöver en kodbas.
Grundreglagen kan återanvändas via befintliga entitets-ID:n och panelens
registeruppslagning. I ett isolerat Configure-prov bevarades `show_panel` och
`savings_kwh_per_c_hour` vid sparning.

En senare port av panelen behöver anpassa prisplan, förklaringar och inställningar
till vårt Configure-flöde och våra sensorattribut. Panelens skrivande reglage och
besparingsuppskattning behöver granskas var för sig. Prioritera hjälptexter och
driftförklaringar. Kandidaten använder sitt nuvarande HA-formulär. [v2.3.0-koden](https://github.com/JohanAlvedal/PumpSteer/tree/v2.3.0/custom_components/pumpsteer)

Husmodellen återanvänds från Heat Pump Pilot. Källrevision och Apache 2.0-licens
finns i den vendorerade koden. [Källhänvisning](../custom_components/pumpsteer/pilot_vendor/__init__.py)
