import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from baton import comparison


def record(case, condition, total, outcome='correct', family='test'):
    return {'case_id':case, 'condition':condition, 'family':family,
            'judgements':[{'outcome':outcome, 'supported':outcome=='correct', 'evidence':'人工核对原文与回答'}],
            'measurement':{'measurement_finalized':True,'models':['same-model'],
                           'usage':{'input_tokens':total-10,'output_tokens':10,'total_tokens':total,'cached_input_tokens':0}}}


def trio(case='a', full=100, brief=30, baton=60, family='test'):
    return [record(case, 'full', full, family=family), record(case,'brief',brief, family=family),record(case,'baton',baton,family=family)]


class ComparisonTest(unittest.TestCase):
    def test_matched_token_ratio_uses_totals(self):
        result=comparison.summarize(trio(full=100,baton=50)+trio('b',full=1000,baton=800))
        self.assertAlmostEqual(result['conditions']['baton']['token_saving_vs_full'],1-850/1100)
        self.assertEqual(result['matched_cases'],2)

    def test_negative_savings_are_preserved(self):
        result=comparison.summarize(trio(baton=120))
        self.assertAlmostEqual(result['conditions']['baton']['token_saving_vs_full'],-0.2)

    def test_abstentions_are_not_answered_correctly(self):
        rows=trio()
        rows[1]['judgements'][0].update(outcome='abstain',supported=False)
        brief=comparison.summarize(rows)['conditions']['brief']
        self.assertEqual(brief['question_accuracy'],0)
        self.assertEqual(brief['abstained'],1)
        self.assertEqual(brief['case_success_rate'],0)

    def test_unreviewed_points_keep_accuracy_unknown(self):
        rows=trio()
        rows[1]['judgements'][0].update(outcome=None,supported=None,evidence='')
        self.assertIsNone(comparison.summarize(rows)['conditions']['brief']['question_accuracy'])

    def test_missing_or_incomplete_usage_never_becomes_zero_cost(self):
        for value in (None, {'measurement_finalized':False}):
            rows=trio()
            rows[2]['measurement']=value
            result=comparison.summarize(rows)
            self.assertIsNone(result['conditions']['baton']['receiver_total_tokens'])
            self.assertIsNone(result['conditions']['brief']['token_saving_vs_full'])
            self.assertFalse(result['token_comparison_complete'])

    def test_mixed_models_do_not_produce_a_saving_ratio(self):
        rows=trio()
        rows[2]['measurement']['models']=['different-model']
        self.assertIsNone(comparison.summarize(rows)['conditions']['baton']['token_saving_vs_full'])

    def test_missing_and_duplicate_arms_are_rejected(self):
        for rows in (trio()[:2],trio()+[copy.deepcopy(trio()[0])]):
            with self.assertRaises(ValueError): comparison.summarize(rows)

    def test_different_question_counts_or_sources_are_rejected(self):
        rows=trio()
        rows[1]['judgements'].append(copy.deepcopy(rows[1]['judgements'][0]))
        with self.assertRaises(ValueError): comparison.summarize(rows)
        rows=trio()
        for row in rows: row['source_sha256']='same'
        rows[2]['source_sha256']='different'
        with self.assertRaises(ValueError): comparison.summarize(rows)

    def test_families_are_reported_separately(self):
        result=comparison.summarize(trio(family='local')+trio('b',family='public'))
        self.assertEqual(set(result['by_family']),{'local','public'})
        self.assertEqual(result['by_family']['public']['matched_cases'],1)

    def test_total_tokens_do_not_add_cache_subset(self):
        rows=trio()
        rows[0]['measurement']['usage']['cached_input_tokens']=80
        result=comparison.summarize(rows)['conditions']['full']
        self.assertEqual(result['receiver_total_tokens'],100)
        self.assertEqual(result['cached_input_tokens'],80)


if __name__=='__main__': unittest.main()
