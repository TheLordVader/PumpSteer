# PumpSteer Thermal Intelligence – plan för passivt HA-prov

Status: **förberedelse**, 2026-10-28. Ingen punkt nedan är utförd i produktion.
Kandidaten är `experiment/thermal-pilot-integration-20261027` på
`8fb0ee9b4dad9f34a7e36e72747d61bffeb6ecbd`. Den inbyggda versionstexten
2.2.4 identifierar inte den lokala experimentkoden; använd commit och filhashar.

## Kända fakta och villkor före start

- Officiell `main` och `v2.2.4` pekade båda på
  `977d5922f505da4f0ed9e21ddd5609ea396ae5ed` vid läsande kontroll av GitHub.
  Den är förfader till kandidaten. Kandidaten har lokala ändringar, inklusive
  en refaktorerad gemensam styrmaskin; identisk versionstext är inte paritet.
- Den separat föreslagna UTC-/tidsomställningsrättelsen finns inte i kandidaten.
  Den skulle påverka aktiv styrning och får inte införas utan eget beslut.
- En tidigare lokal HA-baseline innehåller PumpSteer-manifest 2.2.3. Det är
  historiskt underlag, inte bevis för dagens installerade kod eller HACS-källa.
  Känd baseline från 10 oktober beskriver en läst export, inte en aktuell driftkontroll.
- Före installation krävs särskilt godkänd läsning av nuvarande installerat
  PumpSteer-träd och dess SHA-256, HA-version, HACS-repository och versionsval,
  aktuella options, berörda sensor- och brytartillstånd samt relevanta loggar.
  Identifiera konfigurationskatalogens verkliga sökväg och kontrollera att
  kod, backupväg och återställningsmetod är åtkomliga. Om de avviker från
  underlaget ska skillnaden bedömas innan något ersätts.
- Bekräfta att den befintliga värmeanläggningens dokumenterade säkra väg vid
  avbruten HA/Ohmigo-drift är tillgänglig. En servicebekräftelse bevisar inte
  fysisk verkan. Inga experimentella styr- eller bortfallskommandon används.
- Jämför aktuell officiell main/release och eventuella säkerhetsändringar på nytt
  vid verkställande. Om installerad kod har ointegrerade lokala säkerhetsfixar,
  eller backup/återställning inte kan verifieras, installera inte kandidaten.

## Backup och installation – först efter uttryckligt GO

1. Läs av befintlig aktiv konfiguration och aktuell drift. Dokumentera privat
   PumpSteers entry-ID, options, relaterade entities, Push och Relay Guard samt
   ordinarie HA-status. Spara inte credentials i Git eller rapporter.
2. Skapa en privat, åtkomstskyddad kopia av hela installerade
   `custom_components/pumpsteer`, inklusive filmetadata och SHA-256 per fil.
   Verifiera att kopian är läsbar och matchar originalet. Bekräfta en färsk HA-
   backup som omfattar `.storage`, options/register och Recorder; kontrollera
   åtkomst till backup och återställningsnyckel utan att påstå provad restore.
   Bevara befintliga PumpSteer-Store-filer; ersätt inte databasen.
3. Bygg ett privat kandidatpaket från exakt angiven commit. Kontrollera hash för
   alla integrationsfiler inklusive vendorkod och jämför med det installerade
   trädet. Kandidaten är inte publicerad och en HACS-version med samma nummer
   kan vara en annan kod: använd ingen HACS-installation utan verifierad exakt
   källa. Automatisk HACS-överskrivning under provet måste vara utesluten eller
   särskilt hanterad med godkännande. Använd inte två PumpSteer-kopior/domäner.
4. Vid avtalat tillfälle och efter godkännande för filbyte **och HA Core-omstart**:
   lägg kandidatens kompletta katalog på den verifierade installationsplatsen,
   kontrollera hash och starta om Core enligt installationens befintliga rutin.
   Enbart integrationsreload är inte en verifierad metod att byta importerad
   Pythonkod. Ändra inte HACS-konfigurationen eller andra integrationer.
5. **Steg A, TI av:** kontrollera att options fortfarande har TI av, tidigare
   entity-ID och inställningar finns kvar, ordinär styrning och färska indata
   återkommer, samt att befintliga Push- och Relay Guard-tillstånd stämmer.
   Kontrollera SAFE/felindikering och loggar utan att avsiktligt skapa SAFE
   eller manövrera hårdvara. Jämför observerad output och normala kommandon med
   aktuell driftkontext; olika framtida väder och priser ger inte exakt facit.
6. **Steg B, TI på:** bara om A är godkänt, aktivera TI i befintliga options;
   ändra inga andra val. Bekräfta passiv planstatus, eventuell orsak till avstående
   och att ordinarie utgång/Push/Guard följer samma styrväg. Under identiska
   indata är exakt AV/PÅ-paritet och inga extra kommandon styrkt i isolerad HA;
   en faktisk driftsekvens kan inte ensam bevisa ett kontrafaktiskt alternativ.
   Avstå från att påstå fysisk mätfärskhet eller verifierad besparing.

## Uppföljning och stopp

Använd befintliga HA-tillstånd, loggar och tillgängliga systemverktyg manuellt
på överenskomna tidpunkter: först ordinarie baslinje, därefter efter start och
några representativa regler-/planeringscykler. Ingen ny insamlare eller
återkommande REST-pollning. Kontrollera ledig disk, processens CPU och RSS,
HA:s rapporterade eventloopfördröjningar, loggfrekvens, aktuella TI-attribut,
Recorder-tillväxt och Pilot-Store-filstorlek/ändringstid. Jämför liknande last,
väder- och prisförhållanden; verifiera vilka verktyg som faktiskt finns först.
Optimering sker på befintlig cykel, normalt högst en gång per 30 minuter när
planen inte ogiltigförklaras eller omplanering begärs. Avvikelser utreds; ett
fåtal manuella observationer är inte en kontinuerlig frekvensmätning.

Mätvärden från lokal HA, inte produktionsgränser: 12 timmars optimering tog
596–642 ms med en kärna under beräkningen; eventlooptimerns max var 16–47 ms
utan debug. Identifiera ihållande försämring mot den uppmätta lokala
produktionsbaslinjen och konsekvenser för HA:s ordinarie funktion, inte en
universell CPU-procent som inte har verifierats på denna maskin.

Granska prognosernas utfallspar, MAE och bias endast när ett autentiskt senare
utfall med jämförbar faktisk styrning finns. En aldrig utförd alternativ
Pilot-plan är inte bevisad besparing eller en kausal modellkontroll. Planer kan
bli historiska; rapportera otillräckligt underlag tydligt. Recorder avsiktligt
utelämnar stora planattribut, även om de syns i aktuellt HA-tillstånd.

**Avbryt steg B / slå TI av** vid ett faktiskt extra styrkommando eller annan
oförklarad styravvikelse, TI-relaterat fel som påverkar ordinarie reglering,
upprepad felaktig återstart/återställning, omotiverade täta optimeringar,
växande lagring eller uppmätt HA-belastning som fördröjer ordinarie tjänster.
Konstaterad avvikelse i Push, Relay Guard eller fysisk värmesäkerhet hanteras
omedelbart enligt befintliga säkerhetsrutiner, inte med experimentstyrning.
Saknade prognoser och ärliga avståenden är i sig inte fel. Vid osäkerhet om
orsaken, håll TI av medan ordinarie styrning och dess säkerhet kontrolleras.

Godkänd passiv observation innebär att steg A och B uppfyller ovanstående,
att ordinarie kontroll och säkerhetsfunktioner fortsätter fungera, samt att
resurser inte uppvisar oacceptabel faktisk försämring. Det innebär inte att
modell eller ekonomisk nytta är validerad, och kräver inget påhittat antal
utfall eller någon ändring av styrningen.

## Minsta återställning

- Slå TI **av** i PumpSteers options om HA fortfarande fungerar. Bekräfta i
  befintlig diagnostik och kontrollera därefter ordinarie styrning och loggar.
  Pågående executorarbete kan avslutas i bakgrunden; dess resultat får inte
  användas eller skriva efter unload. Avstängning är inte ett tvångsstopp av
  en Pythontråd.
- Om kandidaten även med TI av orsakar fel: verifiera aktuella hash och återför
  den skyddade kopian av **exakt tidigare PumpSteer-kod**, inklusive metadata,
  efter separat godkänd åtgärd eller inom ett uttryckligt rollbackmandat. Byt
  endast integrationskatalogen och starta om Core enligt godkänd rutin; jämför
  hash och kontrollera tidigare entities, options, styrfunktion och loggar.
- Bevara Recorder, befintlig modellhistorik, register och konfigurationsposter.
  Återställ bara PumpSteer-specifika options som faktiskt ändrats av provet,
  och endast efter kontroll av aktuell konfiguration; återställ inte en gammal
  hel `.storage` eller databas över ny historik. Behåll även eventuell ny
  `pumpsteer.pilot.<entry_id>`-Store privat och orörd vid kodrollback.
- Vid fysisk avvikelse gäller den befintliga dokumenterade säkra signalvägen;
  kodrollback ensam garanterar inte återställning av Ohmigo eller värmepump.

## GO som krävs

Separat uttryckligt GO krävs för: aktuell avgränsad produktionsläsning och
privat backup; installation av exakt kandidat och HA Core-omstart; verifiering
med TI av; aktivering av TI i passivt läge och läsande uppföljning; samt
avstängning och minimal rollback inklusive eventuell ny Core-omstart vid fel.
Ingen push, PR, ändrad hårdvarukonfiguration eller aktiv provstyrning ingår.
