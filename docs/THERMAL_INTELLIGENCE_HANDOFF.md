# Thermal Intelligence — säker utvecklingscheckpoint 2026-10-26

**Avancerad i sin enkelhet.** Målet är en generell, självlärande och förklarbar
assistans till befintlig PI, med befintliga temperaturgivare, väder och priser.
Detta är en överlämning av experimentell kod, **inte modellgodkännande, verifierad
styrnytta eller tillstånd att börja ett verkligt prov**. Utvecklingen är stoppad.

## Git och bevarande

- Publiceringsbranch: `experiment/thermal-intelligence-checkpoint-20261026` i
  `TheLordVader/PumpSteer`, med samma namn på `origin`.
- Kodcheckpoint: **`8d0a0d435b27ae6c9f3e6ad362d581d59726de69`**. Push och
  `git ls-remote origin refs/heads/experiment/thermal-intelligence-checkpoint-20261026`
  verifierades till denna SHA före dokumentcommitten. Slutlig dokumentrevision
  identifieras med `git rev-parse HEAD` och måste matcha remote-SHA vid återupptagning.
- Verkligt gemensamt repository är `../pumpsteer/.git`; publiceringsarbetskopian
  är `../pumpsteer-ti-checkpoint`. Den ursprungliga utvecklingskopian
  `../pumpsteer-irregular` är ren på `experiment/thermal-intelligence-complete-20261026`,
  lokal checkpoint **`fce78bd9a8fc7f5a4bfbc2f247cea38b1eb3da3b`**.
- Publiceringscommitten har redan offentlig `e1a7440278962742858d4571958119a325583c20`
  som faktisk parent. Den bevarar kodträdet från lokal checkpoint, med endast
  dokumentavgränsning och anonymiserade offline-ID-/tjänsteexempel.
  Integrationsfilerna är byte-identiska. Privat lokal historik har inte pushats,
  skrivits om eller raderats; inga befintliga brancher har flyttats.
- Tidigare lokala spår finns kvar: `experiment/thermal-irregular-intervals-20261026`
  vid `2f3467869c76627d397a55fc00b608e0b71479ad`,
  `experiment/thermal-passive-reference-20261026` vid
  `c00fc60a1b6e1169ffaa8b041fff5c013c83a7b8`,
  `experiment/thermal-intelligence-passive-20261026` vid
  `233150f4431f9bfcccc7554955ee3f91047ea808`,
  `experiment/thermal-realism-audit-20261026` vid
  `733e7f5d4e2f0ab699d10026022cc71924a0931e` och
  `experiment/thermal-shadow-verification-20261026` vid
  `4bbca591a56cc6b040137add708bd3711fe41683`.
- Privat underlag ligger utanför Git i den befintliga syskonkatalogen
  `../pumpsteer-ti-complete-20261026/`. Börja med `PRIVATE_HANDOFF.txt` där.
  Den pekar ut originalarkiv, privat historik och exakta lokala körresultat.
  `docs/THERMAL_HISTORY_EVALUATION.md` och `docs/THERMAL_OBSERVATION_TRIAL.md`
  finns bara i originalspåret; de ska inte återinföras i publiceringshistoriken.

## Faktiskt implementerat och modulernas ansvar

| Modul | Ansvar |
| --- | --- |
| `thermal_model.py` | Befintlig modell utökad med begränsad effektiv respons: `dTin/dh = a + b*(Tout-Tin) + c*(-fake_outdoor)`. Tidsviktad treparametersanpassning, oberoende variation, robust residualfilter och domängränser. Högst 240 intervall; initial fit och därefter högst daglig omprövning. Legacy-k behålls för kompatibilitet, används inte som reservkalibrering för nya responsprognoser. |
| `thermal_observer.py` | Kvalificerar verkliga ändpunkter och faktisk tid, 10–120 minuter. Behåller ankare under normal rapporttystnad, bryter vid belagda avbrott/SAFE/käll- eller kontextbyte. Referensen kräver återbekräftelse. Avvikande intervall skyddas från kalibrering. |
| `forecast.py` | Återanvänder befintliga väder-/prisdata i oföränderliga snapshots med UTC, enheter, ursprungs-/mottagningstider och täckningskontroller. Inga efterhandsväder eller påhittade prisintervall. |
| `thermal_intelligence.py` | Fryser 30/60/120-minutersprognoser och jämför med senare verkliga rapporter och persistens. Kopierar känd PI-konfiguration och I-/feltillstånd till isolerad `control.PIController`; räknar framåt utan att ändra riktig PI. Saknat PI-underlag ger uttrycklig enklare hållen-korrigering-referens. |
| `thermal_assist.py` | Begränsade passiva rekommendationer för förvärmning, mindre förvärmning, bromsning och återhämtning. Högst ±1 °C virtuell justering, högst 30 minuter, noll vid bristande evidens. `effective` är alltid false. Endast separat offlinehjälpare kan simulera tillämpning. |
| `sensor.py` | Efter befintlig styrberäkning: överför redan kända indata/PI-tillstånd, återanvänder redan hämtad väderprognos, exponerar befintliga diagnostikattribut och säker restore av fit/källdefinition. Läser aldrig rekommendationer tillbaka till styrningen. |
| `options_flow.py`, `strings.json`, `translations/en.json`, `translations/sv.json` | Beständig Ja/Nej-inställning och översättning. Inga nya obligatoriska givare eller modellparametrar. |
| `scripts/thermal_observation.py`, `thermal_observation_evaluate.py` | Befintlig extern insamlare/utvärderare återanvänds: ursprungliga prognoser, snapshot-ID, modellrevision, driftläge, mätålder, råd och senare utfall. Utvärderaren räknar på loggade original, utan modellomkörning. |
| `tests/thermal_complete_validation.py` och riktade tester | Ändlig kausal tvånodssimulering med riktig befintlig PI och simulerad hårdvara, separat träning/utvärdering samt livscykel-, kvalificerings-, UI-, väder- och shadowprov. |

Modellen lär **effektiv observerad respons under jämförbara förhållanden**,
inte entydig fysisk värmeförlust, verklig pumpenergi eller en viss värmekälla.
Normal drift, förvärmning, bromsning och återhämtning får bidra endast där data
är identifierbara och modellen stöder kontexten. Rampskiften kan avbryta fönster.

## UI och avsiktliga avgränsningar

Fråga: **”Använder du ibland en extra värmekälla?”** Standard **Nej**.
Beskrivning: ”Exempelvis braskamin, luftvärmepump eller annan tillskottsvärme.
PumpSteer kan automatiskt försöka ta hänsyn till extra uppvärmning.”

Ja tillåter den försiktiga etiketten `suspected_extra_heat`; Nej använder enbart
allmän avvikelsediagnostik. Båda skyddar kalibrering och avstår vid osäkra data.
Ingen källa namnges, `verified` förblir false. Äldre assistansoption kan inte
aktivera styrning. Befintliga sensorer/attribut återanvänds; inga sol-, lux-,
markis-, kamin- eller extra obligatoriska entiteter har tillkommit.

Väder påverkar framtida ute-/innetemperatur under känd prognostäckning. Framtida
priser används för begripliga prisindikatorer, inte påstådd faktisk besparing.
Prognosen antar fortsatt känt läge/börvärde/bromsfaktor/förvärmning; framtida
prisutlösta lägesbyten och fysisk pumpvärme är inte kända. Ekonomisk helplanering,
optimalt startögonblick och verifierad återhämtningstid är **inte färdiga**;
`advance_minutes=0`. Ingen aktiv assistans finns.

## Säkerhet, mättider och verifiering

PI, komfortgränser, SAFE, Ohmigo och Relay Guard har alltid företräde. Ingen
ändring av `control.py`, `ohmigo.py` eller `settings.py`; tidigare AST-kontroll
visar oförändrad aktiv beräkning förutom passiv snapshotinsamling. Intern
slutgranskning fann ingen rådgivning-till-styrning-väg. Inget produktionssystem,
ingen tjänst eller HA-instans har kontaktats/installerats/startats om i denna omgång.

HA `last_reported`, mottagningstid och fysisk mättid hålls isär. Fysisk tid är
okänd när givaren inte bevisar den. Restore, cached data och upprepade callbacks
skapar inga nya observationer. Startup/reload/källbyte/bortfall bryter ofärdiga
intervall och prognoser. Fit får återställas endast med kompatibel definition;
aktuell färskhet, väntande prognoser och bekräftelse återställs aldrig som färska.

Originalprognos och dess väder/priser/PI/revision fryses före utfallet. Senare
rapport matchas högst 120 sekunder efter måltiden; faktisk horisont bevaras.
Saknat/avbrutet utfall räknas i täckningen. Runtime-resultat hålls per revision,
läge, bromsfaktor och horisont. Separat all-original-statistik behåller även fel
efter senare lägesbyten, så svåra utfall inte rensas bort i efterhand.

## Faktiska resultat vid checkpointen

**667 pytest-tester passerade** både i originalkopian och sanerad publiceringskopia
(senast 6,99 s i den senare). 27 prov av hela simuleringsharnessen ingår.
`git diff --check` och `python3 scripts/check_version.py` passerar (manifest 2.2.3;
versionen har inte ändrats). **Hel Ruff-kontroll passerar inte:** 50 fynd:
35 E702, 14 E701, 1 F401. Harnessen separat lintades grönt; det är inte grönt
för hela kandidaten. Ingen formatering efter fryst körning döljer detta.

Sex tidigare fall kördes om med PI-framåträkning (utvecklingsunderlag), följt av
**exakt en körning av tre i förväg låsta fall**, utan efterjusterade parametrar.
Fyra träningsdygn följs av två utvärderingsdygn; egen kalibrering, inga injicerade
fits. Fysisk tvånodsmodell har okänd/varierande värme, fördröjning, kvantisering,
gles rapportering och avbrott. Väder är utfärdad syntetisk prognos, inte framtida facit.

| Fall | Utfall/utfärdade | Modell-MAE / persistens, °C |
| --- | ---: | ---: |
| slow_5min | 212/219 | 0,1897 / 0,1901 |
| fast_5_15min | 112/175 | 0,5632 / 0,5545 |
| stove_irregular | 38/131 | 0,2284 / 0,3474 |
| lagged_15_30min | 46/138 | 0,1368 / 0,2065 |
| front_dropout_60min | 1/74 | 0,1251 / 0,1000 — otillräckligt antal |
| drift_missing | 14/80 | 0,4453 / 0,3714 |
| **blind_slow** | **100/156 (64,1 %)** | **0,1848 / 0,2060** |
| **blind_fast** | **55/165 (33,3 %)** | **0,3050 / 0,4182** |
| **blind_lagged** | **11/78 (14,1 %)** | **0,2626 / 0,1909** |

Tabellen är beskrivande all-original-sammanvägning över revisioner/lägen/horisonter,
inte en godkänd modellrevision eller kvalitetsgrind. Blindfallen uppdelat per horisont:

| Fall | Horisont, min | Utfall/utfärdade | MAE modell/persistens, °C |
| --- | ---: | ---: | ---: |
| blind_slow | 30 | 0/52 | ej beräkningsbart |
| blind_slow | 60 | 51/52 | 0,0993 / 0,1431 |
| blind_slow | 120 | 49/52 | 0,2739 / 0,2714 |
| blind_fast | 30 | 1/55 | 0,3674 / 0,7000 — ett utfall |
| blind_fast | 60 | 53/55 | 0,3011 / 0,3868 |
| blind_fast | 120 | 1/55 | 0,4501 / 1,8000 — ett utfall |
| blind_lagged | 30 | 0/27 | ej beräkningsbart |
| blind_lagged | 60 | 11/26 | 0,2626 / 0,1909 |
| blind_lagged | 120 | 0/25 | ej beräkningsbart |

70 fitrevisioner över de tolv utvecklingsarmarna och 32 över de sex blindarmarna.
**Noll kvalificerade råd, noll simulerade ingrepp**, även under träningen. Komfort,
energi och kostnad är därför exakt lika mellan TI och ordinarie PI: ingen visad
assistansnytta eller besparing. Syntetisk baseline har själv komfortavvikelser:
blind_fast 24,80 gradminuter under golvet, blind_lagged 5,84 över taket.
Detta är inte bevis att modellen förbättrat eller certifierat komfort.

Första inomhusrapportens prognosbeslut avstod 236/288, 73/128 respektive 60/87 gånger
i blindfallen. Asynkrona senare utfärdanden räknas separat. Vanliga orsaker:
obekräftad/ej identifierad respons, otillräcklig prognoskvalitet, okänd/osäker
kontext och väntan på nästa ordinarie utfärdande. Råd avstod alltid.

Värmedetektion i blindutvärderingen: TP=FP=FN=0, kvalificerade TN=46,
181 avståenden (2 med värme). Två värmepulser missades genom avstående.
I sex utvecklingsfall: TN=131, 493 avståenden, sex missade pulser.
Ingen detektionsfördröjning kan anges för oupptäckta pulser. Avstående är inte
TN eller bevisat frånvaro av extra värme. Ett tidigare bokföringsfel som räknade
obekräftade intervall som TN rättades före dessa körningar; originalråloggar bevarades.

Privat äldre kausal sjudagarsuppspelning gav ingen egen fit/prognos. Den körningen
föregår slutlig PI-väg och är inte en ny verifiering av denna. Arkivet saknar
ursprungliga framtida väder-/prisprognoser och tillräckligt PI-underlag för att
styrka aktuell väg historiskt. **Ingen verklig prognos- eller besparingsnytta är visad.**
En privat residual-initieringsprototyp prövades och förkastades; den infördes inte.

Frysta körningarnas sju modellmoduler har SHA-256
`120c084611a69581d6bfc6388f2dfd3a160805c212249a7c011e50f1b32ce671`.
Låsta fallmanifestets SHA-256 är
`05283fbdeb94a9573c92eaca0ee6a778384b9815c31311155f7793065bbe3fe8`.
Alla körningars journalintegritetskontroller var utan fynd. Fullständiga grupper,
bias, utfall och avståendeskäl ligger privat i `holdout/run-2/summary.json` och
`holdout/blind-run-1/summary.json`; syntetiska sammanfattningar är i `readout.txt`.

## Kända brister och nästa konkreta steg — påbörjas inte nu

Intern custom-reviewer fann två konkreta kvarstående P2-fel:

1. `thermal_intelligence.py`, `PIProjection.project`: `normal`/`holiday` med
   positiv bromsfaktor under avklingning fryser inte integral/lägger inte på
   bromsöverlagring som riktig PI-väg. Prognosen kan överskatta återhämtning.
2. `ThermalDiagnostics.observe` kräver först giltig hållen-korrigering-projektion,
   även när vald PI-bana vore giltig. Det kan orsaka onödiga avståenden.

**Nästa avgränsade utvecklingssteg:** rätta dessa två PI-paritetsfel med riktade
regressioner mot faktisk styrkod, utan att justera modellens kvalitetsgrindar.
Bedöm därefter på nytt om prognosnytta och täckning motiverar vidare arbete.
Blindfallen är nu sedda; en framtida omkörning är regression, inte nytt blint bevis.

Okänd ordinarie värmetillförsel, sensordrift och extern värme kan skapa samma
observerbara förlopp. Det är en informationsgräns, inte löst källidentifiering.
Osäkerhetsband är empiriska, inte kalibrerade sannolikhetsgarantier. Efter minst
20 utfall används samma revisions/läges största tidigare fel +0,1 °C som empirisk
marginal; före det används brusbaserad marginal. Historisk kvalitet garanterar
inte komfort vid nya styrförhållanden. Gles rapportering ger låg utfallstäckning.

Mätt i blindharnessen: cirka 0,55–0,59 CPU-sekunder för shadow per simulerat dygn,
mindre än 0,5 MB uppskattat behållet Python-objektminne och cirka 3,1 kB snapshot.
Detta är inte HA-produktionsprofilering eller uppmätta Recorder-transaktioner.
Insamlaren behåller tidigare mekanismer (30 s läsning/flush, status högst var 5 min,
väderläsning högst var 15 min, checkpoint vid ändring, 64 MiB rådatatak).
Inga nya tjänster/databaser/pollingloopar eller obligatoriska beroenden införs.
Tidigare service-helper är arkiverad för återanvändning och får inte köras implicit.

## Återuppta i ny Codex-tråd

1. Läs globala och arbetscentralens gemensamma `AGENTS.md`, verifiera repo, common Git-dir,
   worktrees, branch, HEAD, status och remote. Experimentgrenen saknar egen
   AGENTS.md: läs den godkända projektfilen via lokal main samt aktuell fork-main
   inför eventuell publicering; importera den inte automatiskt. Upstream-main
   saknade AGENTS.md vid denna kontrollpunkt.
2. Läs denna fil och privata `../pumpsteer-ti-complete-20261026/PRIVATE_HANDOFF.txt`.
   Återskapa inte tidigare utredningar eller kör alla gamla scenarier igen.
3. Kör `python3 -m pytest -q`, `python3 scripts/check_version.py`, `git diff --check`.
   Ruff: `ruff check custom_components/pumpsteer tests scripts` — de 50 kända
   fynden är öppna. Befintlig lokal Ruff finns i tidigare checks-venv under `/tmp`.
4. Återuppta endast ett nytt uttryckligt uppdrag. Inget automatiskt push/PR,
   upstreamkontakt, integration, aktiv assistans eller produktionsåtkomst.

Ett framtida **separat GO** krävs för ett passivt prov på högst 24 h. Då behövs
endast befintlig extern insamlare, frysta sju rena modellmoduler, verifierade redan
valda temperatur-/styr-/väder-/pris-ID:n samt faktiskt matchande PI-konfiguration.
Kontrollera installerad PI-version/paritet innan dess framåträkning används.
Ingen experimentell HA-installation/reload eller styraktivering behövs. Endast
befintliga tillståndsläsningar och den fasta läsande `weather.get_forecasts`-vägen
får användas; inga styrkommandon. Befintlig sluttid, resurstak, privat lagring,
kontrollsummering och explicit stopp bevaras. Återgång innebär att stoppa enbart
insamlaren och behålla data; HA/PumpSteer ska inte behöva återställas. Provets
konkreta installation/start/avveckling ska godkännas då, inte genom denna checkpoint.
