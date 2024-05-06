import logging
from multiprocessing import Event, Manager, get_context
import multiprocessing
from multiprocessing.managers import ListProxy
import os
from abc import abstractmethod, abstractstaticmethod
from datetime import datetime
from functools import partial
from time import sleep
from typing import Any, Callable, List, Optional, Type
import regex as re

from dotwiz import DotWiz

from deepsecrets import PROFILER_ON, logger
from deepsecrets.config import Config
from deepsecrets.core.model.finding import Finding, FindingMerger
from deepsecrets.core.model.rules.exlcuded_path import ExcludePathRule
from deepsecrets.core.rulesets.excluded_paths import ExcludedPathsBuilder
from deepsecrets.core.rulesets.false_findings import FalseFindingsBuilder
from deepsecrets.core.utils.file_analyzer import FileAnalyzer
from deepsecrets.core.utils.fs import get_abspath

# Experimental approach                        
def watchdog_and_logger(progress: Any, event: Any) -> None:
    while True:
        if event.is_set():
            return
        
        logger.debug(f'{progress[0]} tokens processed')
        sleep(0.4)


class ScanMode:
    config: Config
    filepaths: List[str]
    path_exclusion_rules: List[ExcludePathRule] = []
    file_analyzer: FileAnalyzer
    pool_engine: Type
    progress: ListProxy

    def __init__(self, config: Config, pool_engine: Optional[Any] = None) -> None:
        if pool_engine is None:
            self.pool_engine = get_context('spawn').Pool
        else:
            self.pool_engine = pool_engine

        m = Manager()
        self.progress = m.list([0])
        
        # Should potentially fix silent crashes of per-file scanner processes
        self.config = config
        
        self.filepaths = self._get_files_list()
        self.prepare_for_scan()

    def _get_process_count_for_runner(self) -> int:
        limit = self.config.process_count

        file_count = len(self.filepaths)
        if file_count == 0:
            return 0
        return limit if file_count >= limit else file_count

    def run(self) -> List[Finding]:
        final: List[Finding] = []

        bundle = self.analyzer_bundle()
        proc_count = self._get_process_count_for_runner()
        if proc_count == 0:
            return final
        
        event = Event()
        watchdog = multiprocessing.Process(target=watchdog_and_logger, args=(self.progress, event))
        watchdog.start()

        if PROFILER_ON:
            for file in self.filepaths:
                final.extend(self._per_file_analyzer(file=file, bundle=bundle))
        else:
            with self.pool_engine(processes=proc_count) as pool:
                runnable = partial(pool_wrapper, bundle, self._per_file_analyzer, self.progress)
                per_file_findings: List[List[Finding]] = pool.map(
                    runnable,
                    self.filepaths,
                )  # type: ignore
            event.set()
            watchdog.join()

            for file_findings in list(per_file_findings):
                if not file_findings:
                    continue
                final.extend(file_findings)

        fin = FindingMerger(final).merge()
        fin = self.filter_false_positives(fin)
        return fin

    def _get_files_list(self) -> List[str]:
        flist = []
        if not self.path_exclusion_rules:
            excl_paths_builder = ExcludedPathsBuilder()
            for path in self.config.global_exclusion_paths:
                excl_paths_builder.with_rules_from_file(path)

            self.path_exclusion_rules = excl_paths_builder.rules

        for fpath, _, files in os.walk(get_abspath(self.config.workdir_path)):
            for filename in files:
                full_path = os.path.join(fpath, filename)
                rel_path = full_path.replace(f'{self.config.workdir_path}/', '')
                if not self._path_included(rel_path):
                    continue

                flist.append(full_path)

        return flist

    def _path_included(self, path: str) -> bool:
        if self.path_exclusion_rules is None or len(self.path_exclusion_rules) == 0:
            return True

        if any(excl_rule.match(path) for excl_rule in self.path_exclusion_rules):
            return False
        return True

    @abstractmethod
    def prepare_for_scan(self) -> None:
        pass

    def analyzer_bundle(self) -> DotWiz:
        return DotWiz(
            workdir=self.config.workdir_path,
            path_exclusion_rules=self.path_exclusion_rules,
            engines={}
        )

    @abstractstaticmethod
    def _per_file_analyzer(bundle: Any, file: Any) -> List[Finding]:  # type: ignore
        pass

    def filter_false_positives(self, results: List[Finding]) -> List[Finding]:
        false_finding_rules = self.config.rulesets.get(FalseFindingsBuilder)
        if false_finding_rules is None:
            return results
        

        final: List[Finding] = []       
        for result in results:
            good_result = True
            for false_pattern in false_finding_rules:
                if re.match(false_pattern.pattern, result.detection) is not None:
                    good_result = False
                    break
            if not good_result:
                continue

            final.append(result)

        return final


def pool_wrapper(bundle: DotWiz, runner: Callable, progress: ListProxy, file: str) -> List[Finding]:  # pragma: nocover

    start_ts = datetime.now()
    result = runner(bundle, file, progress)
    progress
    if logger.level == logging.DEBUG:
        logger.debug(
            f' ✓ [{file}] {(datetime.now() - start_ts).total_seconds()}s elapsed \t {len(result)} potential findings'
        )
    else:
        logger.info(f' ✓ [{file}] \t {len(result)} potential findings')
    return result
