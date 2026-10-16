# PumpSteer: minsta observationskontrakt och framtida skuggadapter

Design och torrprov, 2026-10-16. Referens/HEAD:
`d010a8d0715d8c25c978f6d0e16302ac09447e05`, branch
`fix/thermal-evaluation-review`. Förkontrollen fann ren arbetsyta och inga
tillämpliga AGENTS.md. Den äldre rapportens blockerade commit är historik.
Produktkod, tester, Git och publicerad referens ändras inte. Underlaget återanvänder
`docs/THERMAL_INTELLIGENCE.md`, `THERMAL_INTELLIGENCE_PHASE2.md` och
`THERMAL_LEARNING_EVALUATION.md`.

## Verifierad grund och konkret datagap

`PumpSteerSensor._do_update` hämtar konfigurerade inne-/utevärden och klocka
[sensor.py:862](<PROJECT_ROOT>/custom_components/pumpsteer/sensor.py:862).
`get_state` returnerar endast `entity.state`: källans attribut och tider tappas
[utils.py:65](<PROJECT_ROOT>/custom_components/pumpsteer/utils.py:65).
Driftläge/faktorer finns i `_set_state`, `_base_attrs` och rampvariablerna
[sensor.py:657](<PROJECT_ROOT>/custom_components/pumpsteer/sensor.py:657),
[1637](<PROJECT_ROOT>/custom_components/pumpsteer/sensor.py:1637).
Attributet PumpSteer kallar `last_updated` är beräkningstid, inte källans mättid.
`ThermalModel.record_temp(now, indoor)` använder redan pollningstid
[sensor.py:882](<PROJECT_ROOT>/custom_components/pumpsteer/sensor.py:882); återanvänd inte
sådan historik som autentiska mätningar och ändra inte befintlig inlärning här.

HA:s State och sensorgränssnitt har ingen generell garanti om fysisk mättid.
Detta kräver dokumenterad information från de två valda källintegrationerna.
En egen attributnyckel med namnet ”timestamp” är inte bevis. Värde och mättid måste
avse samma mätning, även när värdet är oförändrat. Ingen livekälla har kontrollerats.

## Tidskontrakt: verifierade HA-fakta och föreslagen mappning

| Tid | Betydelse | Användning |
| --- | --- | --- |
| Fysisk `measured_at` | När givaren faktiskt mätte; endast källspecifikt verifierad uppgift | Enda tillåtna `TemperatureMeasurement.measured_at` |
| Integrationens `received_at` | När källintegrationen tog emot rapporten, om den exponerar detta | Separat valfri uppgift; annars `None` |
| HA `last_changed` | Senast ändrat tillståndsvärde | Ingen mättid |
| HA `last_updated` | Senast ändrat värde eller attribut | Ingen mättid |
| HA `last_reported` | Senaste registrering i HA, även vid oförändrat tillstånd | `ha_registered_at`; ingen garanti för ny mätning |
| `adapter_read_at` | När PumpSteer läser denna snapshot | Lokal lästid, aldrig ersättning för mättid |
| `calculated_at` | När diagnostikmotorn anropas | Motorns `now`, utfärdande/upplösning, aldrig källtid |
| `context_observed_at` | När färdigt PumpSteer-läge observeras | Episodkontextens tid; inte backdaterad till temperaturens mättid |

HA-tiderna följer [officiell State-dokumentation](https://www.home-assistant.io/docs/configuration/state_object/).
Integrationer kan undertrycka upprepade rapporter; oförändrat `last_reported`
bevisar därför inte att givaren slutat mäta. Ny registrering kan också vara ett
omskrivet cachevärde. `restored` är en varningssignal, men frånvaro av flaggan
bevisar inte färskhet. [Sensorgränssnittet](https://developers.home-assistant.io/docs/core/entity/sensor/)
beskriver återställning och att State-värdets enhet kan skilja sig från native-enheten.
Officiella källor lästa 2026-10-16; stöd i en verklig HA-version måste verifieras före prov.

**Förslag:** medvetna tider normaliseras till UTC; ingen gissning av tidszon eller
klockoffset. Fysiska tider i framtiden, utan tidszon eller med motsägande värde/tid
avvisas. Källans dokumenterade klocksynkronisering, upplösning och eventuell
fördröjning noteras; tillförlitlig är en verifierad egenskap, ingen sannolikhet.
HA-tider eller `now()` fyller aldrig luckan efter saknad fysisk tid.

## Minsta indata, en post per konfigurerad temperaturkälla

O = obligatoriskt för strikt motorinmatning; V = valfritt. Metadata hålls på
adaptersidan eftersom motorns nuvarande API bara bär värde och mättid.

| Fält | Källa, enhet och avsedd tid | Krav och hantering vid lucka |
| --- | --- | --- |
| `source_id` och roll | `cfg.indoor_temp_entity` / `cfg.real_outdoor_entity` ([config_flow.py:35](<PROJECT_ROOT>/custom_components/pumpsteer/config_flow.py:35)); State.entity_id | O. Ingen hårdkodad entity_id. Ny identitet bryter jämförelser |
| `source_identity` | För just dessa två entities: registry `unique_id`/källans config entry där de finns | V som starkare identitet. Samma entity_id kan annars dölja hårdvarubyte; kräver explicit ny session vid känt byte |
| `value_c`, ursprunglig enhet | State.state + `unit_of_measurement`, samma snapshot | O. Normalisera °C/°F/K med [HA:s TemperatureConverter](https://github.com/home-assistant/core/blob/dev/homeassistant/util/unit_conversion.py); saknad/okänd enhet eller ej ändligt tal ger avvisning, aldrig noll |
| `measured_at` | Dokumenterat källattribut för fysisk mättid, UTC | O för respektive strikt observation; annars `None` och endast registreringsdiagnostik |
| `time_origin`, `time_verified` | Dokumenterad bindning mellan värde, tidsfält och faktisk mätning | O. Ingen generell attributheuristik eller manuellt påstående ersätter källverifiering |
| `received_at`, `ha_registered_at` | Integrationsfält om tillgängligt respektive State.last_reported | V. Skilda från fysisk tid och från varandra; frånvarande tider behålls som `None` |
| `adapter_read_at`, `calculated_at` | Aware lokal klocka vid snapshot respektive motoranrop | O för hantering/ålder. Aldrig temperaturens mättid |
| Tillgänglighet/återställning | State saknas, unknown/unavailable, restored, första snapshot efter aktivering | O som status. Vänta på verifierad ny mätning; restored får inte passera på grund av ny HA-tid |
| Driftläge och kontexttid | Färdigt `_attributes.mode`; egen kontexttid efter uppdatering | O som känd/okänd kontext. Behåll rått läge; holiday/summer_mode/error får inte bli normal |
| Broms/förvärmning | Läs `_brake_ramp`, `_preheat_ramp`, relevanta aktuella attribut; dimensionslös faktor/°C-begäran | V, avser aktuell begäran. Inte levererad effekt. Normal med kvarvarande broms kan märkas explicit ramp-out-kontext, inte bevisad fysisk återhämtning |
| `k_per_hour`, fitted, ursprung | Läs full precision från `_thermal_model.k`, `.is_valid`; 1/h, vid utfärdande | O för ny referens, inte för gamla utfall. Felaktigt/saknat k blockerar nya referenser. Fitted betyder inte verifierad husnoggrannhet |
| Modellursprung/revision | `is_valid=False`: fallback; True: återställt eller lokalt fitted. Motorn tilldelar instanslokal revision | O som ärlig etikett. Nuvarande API skiljer inte återställt k från senare fit; uppfinn inte den skillnaden ([thermal_model.py:133](<PROJECT_ROOT>/custom_components/pumpsteer/thermal_model.py:133)) |
| `heating_kind`, värmeantaganden | Första anslutningen har inga verifierade effekt-/värmeunderlag | Sätt `unknown`, inte estimated/known. Inga `ForecastAssumptions` skapas |

Kompressorstatus (bool/enum), eleffekt (W), framledning/retur (°C), soldata eller
värmemätare är framtida valfria källor med egna identiteter och tider. Ingen extra
hårdvara krävs. Kompressor ”på” bevisar inte levererad värme; W el blir inte W värme
utan underbyggt COP, och W värme blir inte modellens °C/h utan effektiv värmekapacitet.
Framledning/retur utan flöde räcker inte för att bestämma effekt.

## Tillåtna vägar och enkel livscykel

Med verifierad mättid går observationen till befintlig `ThermalDiagnostics.observe`
[thermal_intelligence.py:218](<PROJECT_ROOT>/custom_components/pumpsteer/thermal_intelligence.py:218).
Båda temperaturerna måste vara högst 300 s gamla för nya referenser. En äldre eller
saknad uteobservation skickas med sin riktiga tid eller som `None`: den blockerar
nya referenser men inte giltiga inneutfall mot väntande prognoser. Matchning är
måltid..måltid+120 s; värdet och originalprognosen ändras inte. Referensernas
varningar om okänd värme och konstant uteklimat måste synas.

Finns endast mottagnings-/HA-tid får man visa senaste registrerade värde, status,
registreringsålder och observerade rapportbyten. Man får inte hävda fysisk färskhet,
mätfrekvens, strikt prognosfel eller inlärningsbar temperaturhastighet. Motorns API
kan **inte** användas oförändrat för dessa värden genom att döpa om mottagningstiden.
Givaren kan fortfarande vara fullt användbar för befintlig reglering.

Samma verifierade tid/värde är samma observation oavsett läsningar; samma värde
med ny mättid är nytt underlag. Två senaste tid/värde-par räcker för dubblett/
konfliktkontroll. Samma färska uteobservation kan återanvändas utan att förnya dess
tid. Konflikt vid samma tid måste spärras före motorn; äldre källtid avvisas.

Aktivering/reload/källbyte skapar ny motor och `EpisodeTracker(run_id)`. Första
snapshots etablerar vattenmärken; vänta på verifierade mätningar senare än både
starttid och respektive första snapshot-tid. Inget återställs eller spelas upp.
Vid sessionsbyte redovisas antalet väntande som sessionscensurerat innan objekten
släpps, via `pending_count`; inget privat motor-API eller nytt recoverylager behövs.
Det blir aldrig nollfel eller överföring till ny session. Blanda inte kvalitet
mellan sessioner, källpar eller revisioner.
Känt hårdvarubyte bakom samma entity_id kräver ny session. Kontext observeras vid
verkliga uppdateringar; `EpisodeTracker` hanterar >5 min avbrott och klockepoker.
Ingen interpolation eller kontext fylls i mellan observationerna.

## En enda föreslagen första implementation – byggs inte nu

En opt-in skugganslutning, avstängd som standard, med högst två konfigurerade
attributnamn för fysisk mättid. Tomt eller overifierat källkontrakt ger enbart
registreringsdiagnostik. Namnen väljs först efter källverifiering, utan generella
mallar, replay eller tidsrekonstruktionsmotor. Ingen förutsättning om specifikt hus.

1. Direkt efter `now`/`cfg` i `_do_update` (sensor.py:864–865): endast när aktiverad,
   kopiera små fält från de två State-objekten utan await. Frys värde, enhet och tider;
   behåll inte hela State/attributdict över await. Fångstfel isoleras och ger tomt
   diagnostikunderlag, aldrig ändrade regleringsvärden.
2. I slutet av `async_update`, **efter** nuvarande reglerings-try/except
   (sensor.py:848–860): mata snapshot, ny diagnostikklocka, aktuellt färdigt läge och
   läst k/fitted till en liten synkron hjälpfunktion. Separat try/except omfattar all
   diagnostik. Fel får inte hamna i regleringens error-gren eller skriva `_state`,
   PI-, ramp-, ThermalModel-, komfort- eller utgångsvariabler. Ingen diagnostisk I/O.
   `_set_state` ensam räcker inte: safe/error-vägar går delvis förbi den.
3. Observera kontext vid dess riktiga observationstid med `heating_kind=unknown`.
   Backdatera inte det nya läget till en äldre innetid. Okända råa lägen kan blockera
   ny referens via motorns befintliga kontroll, men gamla utfall bedöms fortfarande.
   Begär inga experimentella värmeintervall. Okänd värme blockerar inte passiva
   referenser/utfallsjämförelser. Om värmeantaganden senare finns måste deras giltighet
   och episodens ursprungstäckning verifieras separat; inga automatikförlängningar.
4. Opt-in skapar objekten; opt-out släpper dem. Avstängt läge gör inga nya State-
   läsningar, temperaturkopior eller motoranrop. Befintlig options-listener räcker;
   unload (sensor.py:317) släpper all diagnostik, reload får ny session. Inga extra
   lyssnare, pollningsloopar, workers, databas eller persistens.
5. Visa en liten sammanfattning via [HA:s nedladdningsbara integrationsdiagnostik](https://developers.home-assistant.io/docs/core/integration/diagnostics/):
   aktivering, käll-/tidsstatus, senaste avvisningsorsak, ålder om verifierbar,
   läge, k-ursprung/revision samt utfärdade/väntande/bedömda/missade per horisont.
   MAE/bias märks passiv referens och hålls per revision/session. Inga nya entiteter,
   löpande tillståndsattribut eller Recorder-skrivningar. Återanvänd befintligt
   `hass.data[DOMAIN][entry_id]` för en referens till samma diagnostikobjekt; ta bort
   den vid opt-out/unload. Exportera vitlistad sammanfattning, utan privata configdata
   eller rå mätlogg. Vid endast
   registreringstid: ”Strikt prognosutvärdering saknar verifierad mättid”.

## Externa och okända värmetillskott

**Verifierat kodförhållande:** `ThermalModel.collect_braking_sample` avvisar stigande
eller plan temperatur, men kan acceptera långsam avkylning med okänd extravärme
[thermal_model.py:167](<PROJECT_ROOT>/custom_components/pumpsteer/thermal_model.py:167).
Fit använder `k=-sum(rate*delta_T)/sum(delta_T²)` (rad 234). Värme som motverkar
kylningen kan därför ge ett för lågt observerat k; bromsfaktor bevisar inte nollvärme.
Denna plan ändrar eller skyddar inte retroaktivt befintlig ThermalModel-inlärning.
Skugganslutningen får bara läsa k, aldrig anropa fit, collect eller restore.

**Rekommenderad enkel framtida regel:** håll prognosutvärdering och inlärningsurval
isär med en klassificering och ett villkor, inte en ny modell/tillståndsmaskin:

| Värmekontext | Belägg | Framtida k-inlärning |
| --- | --- | --- |
| `unknown` (normalfall) | Endast temperaturer och styrkontext; inget självständigt avgränsat värmebidrag | Nej; frånvaro av upptäckt tillskott är inte bevis på passiv avkylning |
| `suspected_extra` | Temperaturavvikelse kan motivera hypotes om tillskott **eller modell-/givarfel**, alternativt separat rapport om annan värmekälla | Nej; ingen slutsats om k, eldning eller exakt effekt |
| `loss_assessable` | Verifierade tider, meningsfull inne/utedifferens och stabilt observationsfönster samt oberoende underlag som avgränsar total tillförd/restlagrad värme till försumbart eller kvantifierat bidrag | Endast kandidat till separat framtida granskad inlärning; inte automatiskt tillstånd att passa k |

Första implementationen lär inte parametrar alls. Framtida inlärning måste avstå
vid unknown/suspected, även när observerad kylning är långsam och även utan någon
användarinställning. Giltiga verkliga utfall ska däremot alltid kunna utvärderas;
välj inte bort stora prognosfel för att få bättre kvalitetssiffror. Befintlig PI
reagerar fortsatt på uppmätt innetemperatur och komfortskydd; klassificeringen ger
inga extra styrbeslut. `heating_kind` förblir unknown utan självständig värmeevidens,
och villkorade intervall avstår utan underbyggda antaganden
[thermal_reliability.py:112](<PROJECT_ROOT>/custom_components/pumpsteer/thermal_reliability.py:112).

SYNTHETISKA KONTRAKTSTESTER, 21 °C inne och 5 °C ute vid start, verklig syntetisk
mätning efter 30 min. Scenarionamnet är facit för läsaren, aldrig modellindata:

| Scenario / uppmätt Tin | Observerbart och möjlig hypotes | Kan inte veta | Prognosutvärdering / k-inlärning |
| --- | --- | --- | --- |
| Vanlig avkylning utan identifierat tillskott / 20,7 °C | Avkylning och begärt braking-läge | Att totalvärmen verkligen varit noll | Ja / nej utan oberoende värmeavgränsning |
| Brasa tänds / 21,3 °C | Ökning trots kallare ute; möjligt tillskott eller modell-/givarfel | Att det är eldning eller hur mycket den värmer | Ja / nej |
| Eldning avtar, restvärme finns / 20,95 °C | Långsam avkylning | Om energi kommer från eldstad, radiatorer, golv, byggnad eller annat; när lagren tömts | Ja / nej |
| Sol ger samma förlopp / 21,3 °C | Exakt samma synliga data som brasa | Att skilja sol från eldning med dessa temperaturdata | Ja / nej |
| Normal värmepump + extern värme / 21,3 °C | Lägesbyte braking -> normal och ökad temperatur | Separata värmebidrag eller att extern värme upphörde vid lägesbytet | Ja / nej |

### Framtida ”Kompletterande värmekälla”: pröva nyttan först

Designförslag, valfritt och standard **av**: ”Finns annan uppvärmning som PumpSteer
inte styr, exempelvis luft-luftvärmepump, braskamin, kakelugn eller elradiatorer?”
Omfattar kontinuerlig och intermittent värme. På betyder att utrustningen finns,
inte att den är aktiv; av bevisar inte frånvaro av extravärme. Sol, personer och
lagrad värme ligger dessutom utanför utrustningsfrågan. Grundskyddet gäller i båda lägena.

**Rekommendation:** börja med alternativ A, generell unknown-hantering utan ny
inställning. Alternativ B kan senare ge begripligare förklaringar och hjälpa att
välja kompletterande underlag, men tillför varken mättid, driftstatus eller effekt.
Installationsuppgiften ensam får inte ändra k, prognos, modellval eller PI. Någon
extra inlärningsnytta jämfört med A är ännu inte visad.

Frivillig tidsatt HA-/användarsignal ”Eldning pågår” eller annan värmekällas driftstatus
kan senare ge bättre händelseetiketter. Källa/tid/betydelse ska vara dokumenterade.
On är ingen effektmätning; off/frånvaro utesluter inte restvärme och certifierar inget
passivt fönster. Inga inställningar, entiteter, knappar eller hårdvarukrav införs här.

Behovet av B ska prövas med annars identiska data: av/på får inte ändra utfallsfel,
inlärningsspärr eller styrbeslut. Jämför förklaringar och **oberoende verifierat**
inlärningsurval vid kontinuerlig/intermittent värme, felaktig/saknad/off-signal, sol
som liknar eldning och kvarvarande restvärme. B behöver ge dokumenterad förbättring
utan falskt säkra fönster eller bortsorterade prognosfel; annars behåll A.
Temperaturdata ensamma kan inte identifiera eldstad, skilja värmekällor eller mäta effekt.

## Torrprov och avgränsad verifiering

`dry_run.py` och `dry_run_results.txt` ligger här. Åtta observationsfall och fem
värmescenarier kontrolleras mot befintliga motor-/episodklasser med HA-stubbar.
Verifierad källtillit är ett uttryckligt fixture-antagande, inte ett test av någon
riktig HA-integration. Värmeklassificering/inlärningsbeslut i exemplen är designregler;
framtida adapter, inlärningsgrind och installationsflagga är inte implementerade.

Körning: `PYTHONDONTWRITEBYTECODE=1 python3 .local-audit/thermal-observation-contract/dry_run.py`.
Ingen simulator, kalibrering, full testsvit eller Python-matris behövs för denna leverans.
Provet verifierar separat tid, oförändrade värden, dubbletter, gles utegivare,
lägesbyte, källbyte/omstart och bevarad utvärdering vid okänd extravärme. Eld/sol med
samma synliga data gav samma prognosfel. Det bevisar datamappning och exemplens
konsekvens, inte husnoggrannhet, verifierad värmetillförsel eller färdig HA-anslutning.

## Filer, acceptans, resurskostnad och villkor före observationsförsök

En kommande minimal implementation behöver ändra `sensor.py` (två isolerade krokar,
små privata mappningshjälpare och livscykel), `options_flow.py` (opt-in och högst två
tidsattributbindningar), `strings.json` samt befintliga `translations/en.json` och
`translations/se.json`; lägga till en tunn `diagnostics.py` för nedladdning. Lägg
fokuserade testfall i en adaptertestfil och återanvänd befintliga termiska regressioner.
Ingen ändring behövs i modellkärnor, ThermalModel-inlärning, styrlogik eller utgångar.
Även dokumentationen uppdateras vid en separat godkänd implementation.

Acceptans: avstängt läge gör inga extra käll-/motoranrop; alla utgångar och
styrbeslut är identiska med diagnostik på/av, även vid framtvingat diagnostikfel.
Fångst före await och slutkrok ska hålla ihop rätt uppdateringscykel, även vid
safe/error. Testa tids-/enhetsmappning, dubblett och konflikt, ogiltig/future tid,
300 s åldersgräns, 120 s matchgräns, modelländring, gles data, källbyte, återställda
värden och unload/reload utan läckor eller sammanblandning. Okänd värme ska tillåta
passiva utfall men inte skapa värmeintervall eller ändra k.

Resursuppskattning, inte uppmätt HA-belastning: två State-snapshots och två senaste
mättid/värde-par per befintlig uppdatering, en motor och en episodtracker per aktiverad
instans. Återanvänd gränserna 24 väntande, 64 utfall per horisont och 32 stängda episoder;
ingen råhistorik eller extra versionsregister. Tillkommande adaptermetadata bör vara
några kB, inte en växande tidsserie; motorns faktiska kostnad mäts separat vid
implementation. Ingen ny pollning, persistens eller Recorder-aktivitet.

Före verkligt observationsförsök måste följande underlag verifieras: (1) valda
källintegrationer och HA-version samt identitet/enhet; (2) exakt mättidsfält, koppling
till värdet, tidszon, upplösning och klocksynkronisering; (3) hur oförändrade värden,
cache, återställning, bortfall och källbyte rapporteras; (4) verkliga rapport- och
fördröjningsintervall i förhållande till motorns toleranser; (5) vad k:s fitted-/
återställningsetikett faktiskt visar. Om fysisk tid inte finns räcker nästa åtgärd
att läsa de två integrationernas källdokumentation/kod efter att deras namn har lämnats,
eller använda ett redan tillgängligt dokumenterat mättidsfält. Hitta inte på färskhet.

För framtida bedömning av värmeförlust behövs dessutom tidsatt drift-/värmeevidens
som kan avgränsa även kompletterande och lagrad värme; befintlig kompressorstatus,
framledning/retur och eventuell värmemätning kan hjälpa men ger inga nya hårdvarukrav.
Saknas detta stannar k-inlärning otillgänglig och temperaturutfall kan ändå jämföras.

Efter separat godkännande av implementation **och** ett avgränsat observationsförsök:
mät andel källor med verifierad tid, dubbletter/avvisningar och orsaker, mätålder/
leveransfördröjning, utfärdade/bedömda/missade per horisont och revision, MAE/bias/
värsta underskattade kylning mot jämförbara baslinjer samt verklig resurskostnad.
Räkna inte korrelerade temperaturpunkter som oberoende husexperiment. Bedöm även om
värmekontext ger begripligare felanalys; inget k-uppdateras i detta första försök.
Ingen implementation eller nästa fas startar genom denna leverans.
