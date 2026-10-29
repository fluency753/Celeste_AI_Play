import asyncio
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from orchestrator.model_catalog import normalize_models, load_models, resolve_selection, fetch_models


def model(ident, **extra):
    return {'model':ident,'displayName':ident.upper(),'inputModalities':['text','image'],
            'supportedReasoningEfforts':[{'reasoningEffort':'low'},{'reasoningEffort':'high'}],
            'defaultReasoningEffort':'high',**extra}


class ModelTests(unittest.TestCase):
    def test_catalog_keeps_model_declared_efforts_and_filters_invalid_entries(self):
        result=normalize_models([model('one',isDefault=True),model('hidden',hidden=True),
            model('text-only',inputModalities=['text']),model('future-model',supportedReasoningEfforts=[
                {'reasoningEffort':'ultra'}, {'reasoningEffort':'minimal'}, {'reasoningEffort':'future'},
                {'reasoningEffort':'ultra'}, {'reasoningEffort':None}, {'reasoningEffort':'bad\nvalue'}]),
            model('one'),None])
        self.assertEqual([item['id'] for item in result['models']],['one', 'future-model'])
        self.assertEqual(result['default_model'],'one')
        self.assertEqual(result['models'][0]['efforts'],['low','high'])
        self.assertEqual(result['models'][1]['efforts'],['ultra','minimal','future'])

    def test_empty_catalog_is_an_error_instead_of_guessed_model_names(self):
        with self.assertRaises(RuntimeError):normalize_models([])

    def test_selection_and_effort_must_both_exist_in_catalog(self):
        with patch('orchestrator.model_catalog.load_models',return_value=normalize_models([model('one')])):
            self.assertEqual(resolve_selection('','high'),('one','high'))
            with self.assertRaises(ValueError):resolve_selection('typo','high')
            with self.assertRaises(ValueError):resolve_selection('one','max')

    def test_concurrent_reads_reuse_cache_and_refresh_is_explicit(self):
        fixture=normalize_models([model('one')])
        with patch('orchestrator.model_catalog._cached',None),patch('orchestrator.model_catalog._loaded',0), \
             patch('orchestrator.model_catalog.fetch_models',AsyncMock(return_value=fixture)) as fetch:
            with ThreadPoolExecutor(max_workers=6) as pool:
                results=list(pool.map(lambda _:load_models(),range(6)))
            self.assertEqual(fetch.call_count,1)
            results[0]['models'].clear()
            self.assertEqual(len(load_models()['models']),1)
            load_models(refresh=True)
            self.assertEqual(fetch.call_count,2)


class DiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_read_only_discovery_pages_and_closes_without_creating_threads(self):
        rpc=SimpleNamespace(start=AsyncMock(),close=AsyncMock(),request=AsyncMock(side_effect=[
            {'data':[model('one')],'nextCursor':'page2'},{'data':[model('two')],'nextCursor':None}]))
        with patch('orchestrator.container.ensure_container',return_value='account-fixture'), \
             patch('orchestrator.model_catalog.CodexRPC',return_value=rpc) as factory:
            result=await fetch_models()
        self.assertEqual(factory.call_args.kwargs['container'], 'account-fixture')
        self.assertEqual([m['id'] for m in result['models']],['one','two'])
        self.assertEqual([call.args[0] for call in rpc.request.call_args_list],['model/list','model/list'])
        self.assertEqual(rpc.request.call_args_list[1].args[1]['cursor'],'page2')
        rpc.close.assert_awaited_once()

    async def test_discovery_failure_still_closes_app_server(self):
        rpc=SimpleNamespace(start=AsyncMock(),close=AsyncMock(),request=AsyncMock(side_effect=RuntimeError('offline')))
        with patch('orchestrator.container.ensure_container',return_value='account-fixture'), \
             patch('orchestrator.model_catalog.CodexRPC',return_value=rpc),self.assertRaises(RuntimeError):
            await fetch_models()
        rpc.close.assert_awaited_once()


if __name__=='__main__':unittest.main()
