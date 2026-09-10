"""Legacy title recovery tests, run by the mandatory V2 pytest gate."""
import copy
import json
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch
from zipfile import ZipFile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import agpm_radar_collect as collect
import agpm_radar_report as report
from radar_title_quality import TitleQualityError, apply_title_markup

ROOT = Path(__file__).resolve().parents[2]
CORA = 'Cora Systems shows PMOs how to replace quarterly risk reviews with continuous portfolio intelligence'
URL = 'http://markets.chroniclejournal.com/chroniclejournal/article/getnews-2026-9-3-cora-systems-shows-pmos-how-to-replace-quarterly-risk-reviews-with-continuous-portfolio-intelligence'
MARKUP = (ROOT / 'v2/tests/data/title-cora.html').read_text()


def response(markup=MARKUP):
    return Mock(ok=True, status_code=200, headers={'content-type': 'text/html'}, text=markup)


class TitlePipelineTests(unittest.TestCase):
    def item(self):
        return {'id': 'legacy-cora', 'title': 'User', 'url': URL, 'summary': 'Материал „User“ описывает PMO.',
                'first_seen_at': '2026-09-09T05:00:00Z', 'published_at': '2026-09-03T05:48:03Z',
                'perimeter': 'near', 'source_hits': [{'provider': 'perplexity'}], 'source_count': 1}

    def test_collect_repairs_before_id_summary_and_classification(self):
        for provider in ['perplexity', 'brave', 'openclaw_cli']:
            candidate = collect.Candidate(title='User', url=URL, source_id='web_test', source_title='search', source_url='https://example.org', provider=provider)
            store = {}
            original_id, classify = collect.material_id, collect.classify_perimeter
            with patch('radar_title_quality.requests.get', return_value=response()), patch.object(collect, 'material_id', wraps=original_id) as mid, patch.object(collect, 'classify_perimeter', wraps=classify) as classifier:
                ids, _ = collect.update_materials([candidate], store, '2026-09-09T05:00:00Z')
            item = store[ids[0]]
            self.assertEqual(item['title'], CORA)
            self.assertEqual(mid.call_args.args[0], CORA)
            self.assertEqual(classifier.call_args.args[0], CORA)
            self.assertEqual(item['summary'], 'Материал требует ручного просмотра: ' + CORA)
            self.assertEqual(item['source_hits'][0]['title_quality']['source'], 'jsonld:headline')

    def test_existing_bad_title_is_recovered_from_new_correct_candidate(self):
        candidate = collect.Candidate(title=CORA, url=URL, source_id='web_test', source_title='search', source_url='https://example.org', provider='perplexity')
        item=self.item();item['brief']='Материал „User“ описывает PMO.'
        item['llm_summary']={'status':'success','short_text':'Качественный текст.','agpm_angle':'Смысл для AgPM.'}
        mid=collect.material_id('User',URL,None);store={mid:item}
        with patch('radar_title_quality.requests.get',return_value=response()):
            collect.update_materials([candidate],store,'2026-09-09T05:00:00Z')
        self.assertEqual(store[mid]['title'],CORA)
        self.assertEqual(store[mid]['brief'],f'Материал „{CORA}“ описывает PMO.')
        self.assertEqual(store[mid]['llm_summary']['short_text'],'Качественный текст.')

    def test_collector_rejects_invalid_candidate_and_keeps_valid_neighbors(self):
        for title, provider in [('No title', 'rss'), ('Assistant', 'perplexity')]:
            with self.subTest(title=title):
                rows = [collect.Candidate(title=t, url=u, source_id='feed', source_title='feed', source_url='https://example.org', provider=p) for t,u,p in [
                    ('Portfolio risks', 'https://example.org/before', 'rss'),
                    (title, URL, provider),
                    ('AI governance', 'https://example.org/after', 'rss')]]
                store, notes, stderr = {}, [], io.StringIO()
                with patch('radar_title_quality.requests.get', return_value=response('<html>unavailable</html>')), redirect_stderr(stderr):
                    new, updated = collect.update_materials(rows, store, '2026-09-10T05:00:00Z', notes=notes)
                self.assertEqual([store[mid]['title'] for mid in new], ['Portfolio risks', 'AI governance'])
                self.assertEqual(updated, [])
                self.assertEqual(len(store), 2)
                self.assertEqual(len(notes), 1)
                self.assertIn('TITLE_QUALITY_REJECTED TITLE_QUALITY_GATE', notes[0])
                self.assertIn('no_reliable_html_title', notes[0])
                self.assertIn(URL, notes[0])
                self.assertEqual(stderr.getvalue(), notes[0] + '\n')

    def test_existing_bad_title_without_recovery_is_not_mutated(self):
        candidate = collect.Candidate(title='Portfolio intelligence', url=URL, source_id='feed', source_title='feed', source_url=URL, provider='rss')
        mid = collect.material_id('User', URL)
        store = {mid: self.item()}
        before = copy.deepcopy(store)
        notes = []
        with redirect_stderr(io.StringIO()):
            self.assertEqual(collect.update_materials([candidate], store, '2026-09-10T05:00:00Z', notes=notes), ([], []))
        self.assertEqual(store, before)
        self.assertEqual(len(notes), 1)

    def test_collector_does_not_swallow_unexpected_errors(self):
        candidate = collect.Candidate(title='No title', url=URL, source_id='feed', source_title='feed', source_url=URL, provider='rss')
        with patch.object(collect, 'recover_web_title', side_effect=RuntimeError('unexpected')):
            with self.assertRaisesRegex(RuntimeError, 'unexpected'):
                collect.update_materials([candidate], {}, '2026-09-10T05:00:00Z')

    def test_collection_persists_rejection_in_run_log(self):
        with tempfile.TemporaryDirectory() as temp:
            wiki = Path(temp)
            config = wiki / 'config.yaml'
            config.write_text('sources: []\n')
            bad = collect.Candidate(title='No title', url=URL, source_id='feed', source_title='feed', source_url=URL, provider='rss')
            good = collect.Candidate(title='AI governance', url='https://example.org/good', source_id='feed', source_title='feed', source_url=URL, provider='rss')
            stdout = io.StringIO()
            with patch.object(sys, 'argv', ['collect', '--config', str(config), '--wiki', str(wiki), '--run-id', '2026-09-10']), patch.object(collect, 'collect_web_research', return_value=([bad, good], [])), patch('radar_title_quality.requests.get', return_value=response('<html>unavailable</html>')), redirect_stderr(io.StringIO()), redirect_stdout(stdout):
                self.assertEqual(collect.main(), 0)
            stats = json.loads(stdout.getvalue())
            self.assertEqual(stats['new_count'], 1)
            self.assertEqual(stats['note_count'], 1)
            self.assertIn('TITLE_QUALITY_REJECTED', Path(stats['run_log']).read_text())
            store = collect.load_materials(wiki / 'data/materials.jsonl')
            self.assertEqual([row['title'] for row in store.values()], ['AI governance'])

    def test_report_recovers_without_dropping_or_reordering(self):
        rows = [self.item(), dict(self.item(), id='other', title='AI PMO', url='https://example.org/pmo', source_hits=[])]
        before = copy.deepcopy(rows)
        with patch('radar_title_quality.requests.get', return_value=response()):
            included, skipped = report.filter_hard_missing_web_links(rows)
        self.assertEqual(skipped, [])
        self.assertEqual([r['id'] for r in included], [r['id'] for r in before])
        self.assertEqual(included[0]['title'], CORA)
        self.assertEqual(included[0]['summary'], f'Материал „{CORA}“ описывает PMO.')
        for index, row in enumerate(included):
            self.assertEqual({k:v for k,v in row.items() if k not in {'title','summary','title_quality'}}, {k:v for k,v in before[index].items() if k not in {'title','summary','title_quality'}})
        self.assertEqual(included[1], before[1])
        self.assertEqual(report.page_title_mismatch('User', MARKUP), (True, CORA))
        self.assertEqual(report.page_title_mismatch('AI PMO', '<meta property="og:title" content="AI PMO">'), (False, 'AI PMO'))
        self.assertEqual(report.page_title_mismatch('Cloud costs', MARKUP), (True, CORA))

    def test_cached_fulltext_recovers_with_original_source(self):
        with tempfile.TemporaryDirectory() as temp:
            wiki = Path(temp)
            item = self.item()
            with patch.object(report.requests, 'get', return_value=response()):
                payload = report.fetch_fulltext(item, wiki)
            self.assertEqual(item['title'], CORA)
            self.assertEqual(payload['page_title']['source'], 'jsonld:headline')
            cached_item = self.item()
            with patch.object(report.requests, 'get', side_effect=AssertionError('cache expected')):
                report.fetch_fulltext(cached_item, wiki)
            self.assertEqual(cached_item['title'], CORA)
            self.assertEqual(cached_item['title_quality']['source'], 'jsonld:headline')

    def test_old_body_only_cache_is_refetched(self):
        with tempfile.TemporaryDirectory() as temp:
            wiki=Path(temp); item=self.item(); path=report.fulltext_cache_path(wiki, URL)
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({'status':'resolved','text':'Old cached article body'}))
            with patch.object(report.requests, 'get', return_value=response()) as fetch:
                report.fetch_fulltext(item, wiki)
            fetch.assert_called_once()
            self.assertEqual(item['title'], CORA)

    def test_report_quality_error_stops_before_queue_docx_or_markdown_writes(self):
        with tempfile.TemporaryDirectory() as temp:
            wiki=Path(temp); (wiki/'data').mkdir()
            (wiki/'data/materials.jsonl').write_text(json.dumps(self.item())+'\n')
            queue=wiki/'data/daily-deferred.jsonl'; queue.write_text('')
            argv=['report', '--wiki', str(wiki), '--until', '2026-09-09', '--days', '1', '--output-prefix', 'daily']
            with patch.object(sys,'argv',argv), patch('radar_title_quality.requests.get',return_value=response('<h1>Navigation</h1>')):
                with self.assertRaisesRegex(TitleQualityError, 'TITLE_QUALITY_GATE.*no_reliable_html_title'):
                    report.main()
            self.assertEqual(queue.read_text(),'')
            self.assertFalse((wiki/'reports').exists())

    def test_markdown_and_docx_use_repaired_title(self):
        item=self.item(); apply_title_markup(item,MARKUP)
        item['_radar_review']={'perimeter':'near','verdict':'core','score':20,'reason':'PMO','hits':{}}
        now=datetime(2026,9,9,tzinfo=timezone.utc)
        markdown=report.render_markdown([item],now,now,[item],[])
        self.assertIn('### '+CORA,markdown)
        self.assertNotIn('„User“',markdown)
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'issue.docx';report.add_markdown_to_docx(markdown,path)
            with ZipFile(path) as archive: xml=archive.read('word/document.xml').decode()
            self.assertIn(CORA,xml)
            self.assertNotIn('„User“',xml)

    def test_good_llm_text_is_preserved(self):
        item=self.item(); item['llm_summary']={'status':'success','short_text':'Качественное описание рисков портфеля.','agpm_angle':'The user reviews risks.'}
        before=copy.deepcopy(item['llm_summary']);apply_title_markup(item,MARKUP)
        self.assertEqual(item['llm_summary'],before)


if __name__ == '__main__':
    unittest.main()
