# PumpSteer 3.0 med passiv Heat Pump Pilot

Pilot lär sig hur innetemperaturen reagerar under vanlig drift. Base beräknar
och skickar styrvärden. I demon är Thermal Intelligence passiv och adaptiv
bromsning avstängd. Kandidatens integrationskod kommer från `6dd5f75` och
innehåller ForecastAudit och Store-rättningen från `47d6b77`. Base jämförs
mot `81632a4`.

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

Husmodellen använder observerad rumsvärme även när Base skickar via Generic
Output (GOS) i en installation utan Ohmigo. Transportbyten och kända utskicksfel
bryter pågående intervall. Kommandokalibreringen har fortfarande en GOS-spärr och
kräver separat verifierad mottagardata. [Intervallhantering](../custom_components/pumpsteer/pilot_passive.py)

Den rapporterade värmeandelen beskriver drift eller belastning. En separat
värmemätning behövs för att uttrycka levererad värme i kW. Pilot behöver minst
24 kvalificerade träningstimmar och tolv avslutade intervall i modellens
72-timmarsfönster. Väder och värme måste variera så att deras bidrag kan
skiljas åt. Base kan användas utan värmetelemetri.

### Frivilliga signaler från värmepumpen

- En till/från-pump kan använda en verifierad binär signal som skiljer aktiv
  rumsvärme från verifierat stopp. Lämna signalen otillgänglig under varmvatten,
  avfrostning och okänd drift. Kompressorstatus ensam skiljer inte de lägena.
  En självständig binär källa kan använda HA:s rapporttid i läget Verifierad
  när färska rapporter och bortfall är belagda. En signal som bara meddelar
  värdebyten ger ingen ny täckning under en längre tyst period.
- För en inverter används den befintliga verifierade rumsvärmesignalen i procent.
  En källa med verifierad frekvens eller inverterineffekt kan lämna den andelen
  genom att beräkna `100 × värde / fast verifierad referens` i samma enhet.
  Välj en belastningskälla åt gången. Bevara observationens källtid i
  `measurement_time` och ge varje ändrad källa eller referens ett eget entitets-ID.
  Använd noll för verifierat stopp och markera saknad, motstridig eller okänd
  information som otillgänglig. Råa Hz och W accepteras inte som värmeandel.
- Ett separat valt varmvattenläge måste ha giltig källtid för båda indatatyperna.
  Ett valt radiatorläge har samma krav för procentkällan. En observation upphör att gälla senast
  när den äldsta av de relevanta uppgifterna når den befintliga femminutersgränsen.
  En färsk HA-rapport kan inte förnya en gammal källtid. Råhistorik och färdiga
  prognospar bevaras. Passningen börjar om när samma värmekälla får ett nytt
  krav på en separat lägessignals källtid.
- Verifierad inverterineffekt med enheten W eller kW kan även användas för
  befintlig energidiagnostik inom den mätningens omfattning. Frekvens, elektrisk
  ineffekt och total eleffekt från en separat elmätare är olika signaler.

Verifiera nya givare mot utrustningens egen avläsning vid minst två naturliga
belastningsnivåer, ett stopp och en växling mellan rumsvärme och varmvatten.
Kontrollera att rapporteringen kan skilja nya observationer från cachade eller
återpublicerade värden. Om den endast ger mottagningstid kan värdena beskriva
driften diagnostiskt. Den tidsatta intensiteten hålls då otillgänglig för Pilot.

## Kör demonstrationen lokalt

Testerna använder syntetiska givare och mottagare. De körs från kandidatens
arbetskopia i en separat Python 3.14.10-miljö och behöver ingen HA-server,
produktionskonfiguration eller driftlogg.
Använd en Git-klon med full historik för regressionssviten. Tre tester läser
den äldre Base-referensen `b273afc` med `git show`.

```sh
python -m pip install 'homeassistant==2026.11.2' 'pytest==9.2.0' \
  'pytest-asyncio==1.4.1.post1'
python -B -m pytest -vv -o asyncio_default_fixture_loop_scope=function \
  tests/test_passive_pilot_house.py \
  tests/test_pilot_response_evidence.py \
  tests/test_thermal_passive_faults.py
python -B tests/probes/check_passive_pilot_native_ha.py
python -B tests/probes/check_configure_native_ha.py
python -B -m pytest tests/ -q -o asyncio_default_fixture_loop_scope=function
```

Core-provet följer 52 syntetiska timmar per värmepumpstyp, från HA:s
temperatur- och värmerapporter till modellpassning och frysta prognospar.
Varmvatten, saknad aktualitet och för gammalt driftläge blockerar lärande i
berörda intervall. Provet kontrollerar att prognosen använder den frysta
kurvan vid observationens faktiska tidpunkt och att modellens träningsdata
föregår prognosen.

| Syntetiskt system | Avslutade intervall | Passningsstatus | Pilot–referenspar vid 30 / 60 / 120 min | Matchande Ohmigo-/GOS-anrop med TI av/på |
|---|---:|---|---:|---:|
| Till/från | 45 | `learning` | 6 / 6 / 4 | 1 070 |
| Inverter | 42 | `learning` | 7 / 8 / 3 | 1 070 |

Regressionssviten innehåller 1 760 tester. Den täcker bland annat otillräcklig
variation (`gain_frozen`), GOS utan Ohmigo, transportbyten, gamla och sena
rapporter samt blockerad Store. Configure-provet täcker sparlogiken för alla
94 fält i HA Core 2026.11.2. En separat syntetisk jämförelse mot `81632a4`
kontrollerar åtta cykler med PI, prisbromsning och SAFE samt 16 utskick.

## Vad som återstår att belägga

Det befintliga fältunderlaget saknar kvalificerade Pilot-intervall och
oberoende prognospar för den här modellen. Verklig drift behöver visa att
källan kan skilja rumsvärme från andra lägen och att Pilot slår referensen
på senare, oberoende utfall. ForecastAudit utvärderar accepterade
temperaturändringar. Rapportera därför täckning och bortfall tillsammans
med prognosfelet.

## Passivt fältprov och återgång

1. **Kvalificera en källa.** Bekräfta värmesignalens innebörd, enhet, källtid
   och giltighet under naturlig modulering eller cykling samt varmvatten och
   stopp. Vid okänd aktualitet återstår källverifiering före fältprovet.
2. **Förbered installation och stopp.** Kontrollera faktisk HA Core-version
   och integrationsfiler. Ta en verifierad backup av integrationen, dess
   konfiguration och PumpSteers Store utanför `custom_components`. Bind
   installations- och provrutinen till basens och kandidatens fulla SHA.
   Verifiera stopp vid deadline, återförsök, manuell reservrutin och återgång
   i isolerad miljö. Sätt en sluttid och använd en övervakare.
3. **Bekräfta passivitet.** Börja med TI och adaptiv bromsning av. Följ
   ordinarie styrvärden och, om de finns, Relay Guard och mottagarens värden.
   Efter separat godkänd TI-start ska multiplikatorn vara 1,0 och
   `passive_field_trial_safe` sann. `legacy_adaptive_enabled`,
   `legacy_adaptive_return_pending`, `active_control` och `fit_trusted`
   ska vara falska. Bekräfta att nästa Store-checkpoint lyckas.
4. **Följ naturlig drift.** Arkivera privat före historikrotation. Räkna
   kvalificerade timmar och avslutade intervall, följ passningsstatus och
   redovisa bortfall med orsak. Notera kända externa värmekällor och avbrott
   i utvärderingen med de tidsgränser som faktiskt går att belägga. Jämför
   Pilot och referensen på samma senare observationer per horisont och
   modellrevision, med antal separata episoder och absolutfel.
5. **Verifiera avslut.** Stoppa vid sluttiden eller tidigare vid styravvikelse,
   lagringsfel eller problematisk belastning. Journalför stoppbegäran,
   sparad avstängning och verifierat avstängd runtime var för sig. Vid
   åtkomstbortfall gäller återförsök eller manuell reservrutin. Kontrollera
   båda flaggorna, multiplikatorn och normal styrning. Ett normalt provslut
   kräver bara avstängning av TI.
6. **Återgå vid kandidatfel.** Spara kandidatens Store och provunderlag
   privat före återställning. Återställ verifierade integrationsfiler,
   PumpSteers egna inställningar och dess tidigare Store från backupen.
   Kontrollera efter en separat godkänd start att integrationen är laddad,
   båda funktionerna är avstängda och Base styr normalt.

I det isolerade Store-provet läste kandidaten råhistorik och ett sparat
Pilot-par. `81632a4` läste råhistoriken ur kandidatens Store. Behåll alltid
kandidatkopian eftersom den äldre revisionen saknar stöd för Pilot-paren.

## Kopplingen till Johans v2.3.0

Båda använder HA-domänen `pumpsteer`. Johans panel slår upp entiteter via
config entry och entitetsregistret och ändrar värden via HA:s tjänster för
nummer, brytare och datum. Börja en eventuell senare anpassning med
hjälptexter och visning av drift. Skrivande reglage, prisplanens attribut och
besparingsberäkningen behöver var sin kompatibilitetskontroll mot den kodbasen.
Kandidaten använder sitt nuvarande HA-formulär.
[Panelregistrering](https://github.com/JohanAlvedal/PumpSteer/blob/1011e5de6ab737f8c0832af501b4ddf1b157635b/custom_components/pumpsteer/panel.py) ·
[Reglage](https://github.com/JohanAlvedal/PumpSteer/blob/1011e5de6ab737f8c0832af501b4ddf1b157635b/custom_components/pumpsteer/frontend/pumpsteer-panel.js) ·
[Besparingsberäkning](https://github.com/JohanAlvedal/PumpSteer/blob/1011e5de6ab737f8c0832af501b4ddf1b157635b/custom_components/pumpsteer/savings.py)

## Beroenden och licenser

Integrationen använder HA:s Recorder och har en tom `requirements`-lista.
PumpSteers licens är AGPL-3.0. Husmodellen kommer från Heat Pump Pilot under
Apache-2.0. Den kvarvarande RTM-koden har MIT-licens. Källrevisioner och
licenstexter följer med koden. [PumpSteer](../LICENSE) · [Pilot](../custom_components/pumpsteer/pilot_vendor/__init__.py) · [RTM](../custom_components/pumpsteer/thermal_intelligence/NOTICE)
