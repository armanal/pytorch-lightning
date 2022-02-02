# Copyright The PyTorch Lightning team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator
from copy import deepcopy
from typing import Any, Callable, Generator, List, Optional, Tuple, Union

import torch
from torch.utils.data.dataloader import DataLoader

from pytorch_lightning.trainer.supporters import CombinedLoader, CycleIterator
from pytorch_lightning.utilities.apply_func import apply_to_collection, apply_to_collections
from pytorch_lightning.utilities.auto_restart import (
    _add_capture_metadata_collate,
    _patch_dataloader_get_iterators,
    _teardown_dataloader_get_iterators,
    IteratorState,
    MergedIteratorState,
    patch_dataloader_iterator,
)
from pytorch_lightning.utilities.exceptions import MisconfigurationException
from pytorch_lightning.utilities.imports import _fault_tolerant_training


class AbstractDataFetcher(ABC):

    """This base class should be used to implement a fault tolerant ``DataFetcher``. It is required to override the
    ``fetching_function`` with fetching logic.

    Example::

        class SimpleDataFetcher(AbstractDataFetcher):
            def fetching_function(self):
                while True:
                    try:
                        return next(self.dataloader_iter), False
                    except StopIteration:
                        return None, True
    """

    @abstractmethod
    def fetching_function(self) -> Any:
        """Override with your own fetching logic."""

    @abstractmethod
    def prefetching(self) -> None:
        """Override with your own pre-fetching logic."""

    def __init__(self, prefetch_batches: int = 0) -> None:
        if prefetch_batches < 0:
            raise MisconfigurationException("`prefetch_batches` should at least be 0.")
        self.prefetch_batches = prefetch_batches

        self.dataloader: Optional[Union[DataLoader, CombinedLoader]] = None
        self.dataloader_iter: Optional[Iterator] = None

        self.batch_to_device: Optional[Callable]

        self.batches: List
        self.fetched: int
        self.done: bool

        self.reset()

    def setup(self, dataloader: Iterable, batch_to_device: Optional[Callable] = None) -> None:
        self._add_capture_metadata_collate(dataloader)

        self.dataloader = dataloader
        self.batch_to_device = batch_to_device

        self._attach_data_fetcher()

    @staticmethod
    def _add_capture_metadata_collate(dataloader: Iterable) -> None:
        if not isinstance(dataloader, (DataLoader, CombinedLoader)):
            return

        if isinstance(dataloader, CombinedLoader):
            dataloader = dataloader.loaders

        apply_to_collection(dataloader, DataLoader, _add_capture_metadata_collate)

    def _apply_patch(self):
        def _apply_patch_fn(loader: DataLoader, iterator: Iterator):
            if isinstance(loader, CycleIterator):
                loader = loader.loader
                # cycle_iterator = iterator
                iterator = iterator._loader_iter

            if isinstance(loader, DataLoader) and _fault_tolerant_training():
                loader._lightning_fetcher = self
                patch_dataloader_iterator(loader, iterator, self)

        apply_to_collections(self.loaders, self.loader_iters, (Iterator, DataLoader), _apply_patch_fn)

    def _store_dataloader_iter_state(
        self, dataloader_iter: Iterator, dataloader_iter_states: List[IteratorState]
    ) -> None:
        if getattr(dataloader_iter, "cache_states", None) is None:
            dataloader_iter.cache_states = {}

        if getattr(dataloader_iter, "state", None) is None:
            dataloader_iter.state = MergedIteratorState()

        for iter_state in dataloader_iter_states:
            iter_name = iter_state.name
            if iter_name not in dataloader_iter.cache_states:
                dataloader_iter.cache_states[iter_name] = []
            dataloader_iter.cache_states[iter_name].append(iter_state)

        if self.fetched >= self.prefetch_batches:
            for iter_state in dataloader_iter_states:
                if len(dataloader_iter.state):
                    dataloader_iter.previous_state = deepcopy(dataloader_iter.state)
                iter_name = iter_state.name
                state = dataloader_iter.cache_states[iter_name].pop(0)
                dataloader_iter.state.update(iter_name, state)

    @property
    def loaders(self) -> List[DataLoader]:
        if self.dataloader is None:
            raise MisconfigurationException(
                "The `DataFetcher` should be setup with an instance of a PyTorch ``DataLoader``."
            )
        if isinstance(self.dataloader, CombinedLoader):
            loaders = self.dataloader.loaders
        else:
            loaders = [self.dataloader]
        return loaders

    @property
    def loader_iters(self) -> List[Iterator]:
        if self.dataloader is None:
            raise MisconfigurationException(
                "The `DataFetcher` should be setup with an instance of a PyTorch ``DataLoader``."
            )

        if self.dataloader_iter is None:
            raise MisconfigurationException("The `dataloader_iter` isn't available outside the __iter__ context.")

        if isinstance(self.dataloader, CombinedLoader):
            loader_iters = self.dataloader_iter.loader_iters
        else:
            loader_iters = [self.dataloader_iter]
        return loader_iters

    @property
    def state(self) -> Any:
        def collect_state(iterator: Iterator):
            return iterator.state

        return apply_to_collection(self.loader_iters, Iterator, collect_state)

    def _attach_data_fetcher(self):
        def _attach_data_fetcher_fn(loader: DataLoader):
            if isinstance(loader, CycleIterator):
                loader = loader.loader

            if isinstance(loader, DataLoader) and _fault_tolerant_training():
                loader._lightning_fetcher = self

        apply_to_collection(self.loaders, (DataLoader, CycleIterator), _attach_data_fetcher_fn)

    def __iter__(self) -> Generator[Tuple[Any, bool], None, None]:
        if self.dataloader is None:
            raise MisconfigurationException("The iterate hasn't been provided. HINT: Did you call setup function ?.")
        self.reset()
        self._attach_data_fetcher()
        _patch_dataloader_get_iterators()
        self.dataloader_iter = iter(self.dataloader)
        self._apply_patch()
        self.prefetching()
        return self

    def __next__(self):
        return self.fetching_function()

    def reset(self) -> None:
        self.batches: List = []
        self.fetched: int = 0
        self.done: bool = False

    def teardown(self) -> None:
        self.reset()
        if isinstance(self.dataloader, CombinedLoader):
            self.dataloader.reset()
        if isinstance(self.dataloader, DataLoader):
            CombinedLoader._shutdown_workers_and_reset_iterator(self.dataloader)
        self.dataloader_iter = None
        _teardown_dataloader_get_iterators()


class DataFetcher(AbstractDataFetcher):

    """This class is used to control batch fetching flow. By default, the ``fetching_function`` will pre-fetch a
    batch in advance to detect the end of the iteration.

    Args:
        prefetch_batches: Number of batches to be pre-fetched. Lightning will pre-fetch
            at least 1 batch for tracking the latest batch.
        store_on_device: Whether to store the pre-fetched batches on device.
    """

    def __init__(self, prefetch_batches: int = 1, store_on_device: bool = True) -> None:
        super().__init__(prefetch_batches=prefetch_batches)
        self.store_on_device = store_on_device

    def on_fetch_start(self) -> None:
        """Hook to override to handle the logic before fetching a batch."""

    def on_fetch_end(self, batch, on_fetch_start_output: Optional[Any] = None) -> None:
        """Hook to extend which handles the logic after fetching a batch."""
        self.batches.append(batch)

    def wait(self) -> None:
        """Hook to override to indicate the `DataFetcher` to wait for an event."""

    def prefetching(self) -> None:
        iterator = self.dataloader_iter
        for _ in range(self.prefetch_batches):
            try:
                self._fetch_next_batch(iterator)
            except StopIteration:
                break

    def fetching_function(self) -> Tuple[Any, bool]:
        if self.batches:
            batch = self.batches.pop(0)
        else:
            # empty iterator, no prefetching done
            raise StopIteration
        if not self.done:
            assert self.dataloader_iter is not None
            try:
                self._fetch_next_batch(self.dataloader_iter)
            except StopIteration:
                self.done = True
        self.wait()
        return self.move_to_device(batch), len(self.batches) == 0

    def _fetch_next_batch(self, iterator: Iterator) -> None:
        start_output = self.on_fetch_start()
        batch = next(iterator)
        self.fetched += 1
        self.on_fetch_end(batch, start_output)

    def move_to_device(self, batch: Any) -> Any:
        if self.store_on_device and self.batch_to_device is not None:
            batch = self.batch_to_device(batch)
        return batch


class InterBatchParallelDataFetcher(DataFetcher):

    """This class implements inter-batch parallelism, which aims at hiding the latency of host-to-device copy of
    input batches behind computationally intensive operations.

    code-block::

        Without parallelization:

        batch 0: [HtoD][forward][backward]
        batch 1:                          [HtoD][forward][backward]
        batch 2:                                                   [HtoD][forward][backward]

        With parallelization, the latency of HtoD copy can be hidden:

        batch 0: [HtoD][forward][backward]
        batch 1:       [HtoD]             [forward][backward]
        batch 2:             [HtoD]                          [forward][backward]
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.cuda_stream = torch.cuda.Stream()
        self.events: List[torch.cuda.Event] = []

    def move_to_device(self, batch):
        with torch.cuda.stream(self.cuda_stream):
            return super().move_to_device(batch)

    def on_fetch_start(self) -> "torch.cuda.Event":
        # create a cuda event used to record the async stream of data to device.
        return torch.cuda.Event()

    def on_fetch_end(self, batch, event: torch.cuda.Event) -> None:
        super().on_fetch_end(batch)

        # record event and store the event
        event.record()
        self.events.append(event)

    def wait(self) -> None:
        # pop first event from the queue and wait for the batch to be available on device.
        event = self.events.pop(0)
        event.wait()


class StepFuncDataLoaderIter:

    """This class is a wrapper to keep track of dataloader iterator fetching event while left entirely to user
    control."""

    def __init__(self, iterator: Iterator, data_fetcher: "AbstractDataFetcher"):
        self.iterator = iterator
        self.data_fetcher = data_fetcher

    def __iter__(self) -> "StepFuncDataLoaderIter":
        return self

    def __next__(self) -> Any:
        try:
            data = next(self.iterator)
            self.data_fetcher.fetched += 1
            return data
        except StopIteration:
            self.data_fetcher.done = True
            raise StopIteration


class DataLoaderIterDataFetcher(AbstractDataFetcher):

    """This class is used to return directly the `dataloader_iter` to the ``LightningModule`` training_step for
    users to implement their own pre-fetching logic. This feature can be activated as follows:

    Example::

        Class MyModel(LightningModule):

            def __init__(self):
                self.automatic_optimization = False

            def training_step(self, dataloader_iter: Iterator, batch_idx: int) -> None:
                # it is the user responsibility to fetch and move the batch to the right device.
                batch = next(dataloader_iter)
                batch = batch.to(self.device)
                ...
    """

    def __init__(self):
        super().__init__()
        self.store_on_device = False

    def prefetching(self) -> None:
        self.iterator = iter(StepFuncDataLoaderIter(self.dataloader_iter, self))

    def fetching_function(self) -> Tuple[int, Tuple[Iterator, bool]]:
        if not self.done:
            return self.fetched, (self.iterator, self.done)
        raise StopIteration
