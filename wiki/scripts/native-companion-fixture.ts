// Synthetic copied-SQLite rehearsal and the actual native HTTP read boundary.
import { app } from '../src/index.js';
import { setup, SLUG } from '../test/helpers/fixture.js';
import { makeD1 } from '../test/helpers/d1shim.js';
import { migrateDatabaseCopy } from './migrate-native.js';
import { storedCompanionRecords } from '../src/native-companion.js';
import { DatabaseSync, backup } from 'node:sqlite';
import { readFileSync, writeFileSync, mkdirSync } from 'node:fs';
import { resolve } from 'node:path';
const output=process.argv[2];
if(!output) throw new Error('usage: native-companion-fixture.ts NEW_DIRECTORY');
mkdirSync(output); const directory=resolve(output);
const fixture=JSON.parse(readFileSync(new URL('../test/fixtures/companion-export.json',import.meta.url),'utf8'));
const harness=setup();
let nativeDb: DatabaseSync|undefined;
try {
  harness.db.prepare('UPDATE articles SET wikipedia_title=?,revid=?,version=?,annotations=? WHERE slug=?').run(
    fixture.article.wikipediaTitle,fixture.article.revid,fixture.article.version,JSON.stringify(fixture.article.annotations),SLUG);
  await backup(harness.db,resolve(directory,'before.sqlite3'));
  const report=await migrateDatabaseCopy(resolve(directory,'before.sqlite3'),resolve(directory,'native.sqlite3'));
  writeFileSync(resolve(directory,'migration-report.json'),JSON.stringify(report,null,2)+'\n');
  nativeDb=new DatabaseSync(resolve(directory,'native.sqlite3'));
  harness.env.DB=makeD1(nativeDb);
  globalThis.fetch=async()=>{throw new Error('network forbidden in native integration fixture');};
  const response=await app.request(`/api/article/${SLUG}/companion.json`,undefined,harness.env);
  if(response.status!==200) throw new Error('native HTTP '+response.status);
  const annotations=await response.json();
  const stored=nativeDb.prepare('SELECT annotations,revid,schema_version,version,wikipedia_title FROM articles WHERE slug=?').get(SLUG)!;
  const items=JSON.parse(String(stored.annotations));
  // The route returns each stored link with the item's review facts and the
  // article's website pin projected into the format's own fields.
  const expected=storedCompanionRecords(items,{wikipediaTitle:String(stored.wikipedia_title),revid:Number(stored.revid)}).map(record=>record.annotation);
  if(JSON.stringify(expected)!==JSON.stringify(annotations)) throw new Error('HTTP changed stored native records');
  writeFileSync(resolve(directory,'article.json'),JSON.stringify({...stored,slug:SLUG,annotations:items},null,2)+'\n');
  writeFileSync(resolve(directory,'links.json'),JSON.stringify(annotations,null,2)+'\n');
  process.stdout.write(JSON.stringify({annotations:expected.length,source_items:items.length,migration:report,records_unchanged:true}));
} finally {nativeDb?.close();harness.db.close();}
