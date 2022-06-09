import functools
import logging
import random
import sys
import time

from collections import Counter, defaultdict
from concurrent.futures.thread import ThreadPoolExecutor
from datetime import datetime
from typing import Mapping, List, Sequence, Union

import pandas as pd

from label_sleuth.active_learning.core.active_learning_factory import ActiveLearningFactory
from label_sleuth.analysis_utils.labeling_reports import get_suspected_labeling_contradictions_by_distance_with_diffs, \
    get_disagreements_using_cross_validation
from label_sleuth.config import Configuration
from label_sleuth.data_access.core.data_structs import DisplayFields, Document, Label, TextElement, LABEL_POSITIVE
from label_sleuth.data_access.data_access_api import DataAccessApi
from label_sleuth.data_access.label_import_utils import process_labels_dataframe
from label_sleuth.data_access.processors.csv_processor import CsvFileProcessor
from label_sleuth.definitions import ACTIVE_LEARNING_SUGGESTION_COUNT
from label_sleuth.models.core.model_api import ModelStatus
from label_sleuth.models.core.catalog import ModelsCatalog
from label_sleuth.models.core.model_type import ModelType
from label_sleuth.models.core.models_factory import ModelFactory
from label_sleuth.models.core.prediction import Prediction
from label_sleuth.orchestrator.core.state_api.orchestrator_state_api import Category, Iteration, IterationStatus, \
    ModelInfo, OrchestratorStateApi
from label_sleuth.orchestrator.utils import convert_text_elements_to_train_data
from label_sleuth.training_set_selector.training_set_selector_factory import get_training_set_selector


# constants
NUMBER_OF_MODELS_TO_KEEP = 2
TRAIN_COUNTS_STR_KEY = "train_counts"


# members
new_data_infer_thread_pool = ThreadPoolExecutor(1)


class OrchestratorApi:
    def __init__(self, orchestrator_state: OrchestratorStateApi, data_access: DataAccessApi,
                 active_learning_factory: ActiveLearningFactory, model_factory: ModelFactory,
                 config: Configuration):
        self.orchestrator_state = orchestrator_state
        self.data_access = data_access
        self.active_learning_factory = active_learning_factory
        self.model_factory = model_factory
        self.config = config

    def get_all_dataset_names(self):
        return sorted(self.data_access.get_all_dataset_names())

    # Workspace-related methods

    def create_workspace(self, workspace_id: str, dataset_name: str):
        """
        Create a new workspace
        :param workspace_id:
        :param dataset_name:
        """
        logging.info(f"Creating a new workspace '{workspace_id}' using dataset '{dataset_name}'")
        if dataset_name not in self.data_access.get_all_dataset_names():
            message = f"{dataset_name} does not exist. Cannot create workspace {workspace_id}"
            logging.error(message)
            raise Exception(message)
        self.orchestrator_state.create_workspace(workspace_id, dataset_name)

    def delete_workspace(self, workspace_id: str):
        """
        Delete a given workspace
        :param workspace_id:
        """
        logging.info(f"Deleting workspace '{workspace_id}'")
        if self.workspace_exists(workspace_id):
            workspace = self.orchestrator_state.get_workspace(workspace_id)
            try:
                for category_name in workspace.categories.keys():
                    self._delete_category_models(workspace_id, category_name)
                self.orchestrator_state.delete_workspace_state(workspace_id)
            except Exception as e:
                logging.exception(f"error deleting workspace '{workspace_id}'")
                raise e
            try:
                self.data_access.delete_all_labels(workspace_id, workspace.dataset_name)
            except Exception as e:
                logging.exception(f"error clearing saved labels for workspace '{workspace_id}'")
                raise e

    def workspace_exists(self, workspace_id: str) -> bool:
        return self.orchestrator_state.workspace_exists(workspace_id)

    def list_workspaces(self):
        return sorted([x.workspace_id for x in self.orchestrator_state.get_all_workspaces()])

    def get_dataset_name(self, workspace_id: str) -> str:
        return self.orchestrator_state.get_workspace(workspace_id).dataset_name

    # Category-related methods

    def create_new_category(self, workspace_id: str, category_name: str, category_description: str):
        """
        Declare a new category in the given workspace
        :param workspace_id:
        :param category_name:
        :param category_description:
        """
        logging.info(f"Creating a new category '{category_name}' in workspace '{workspace_id}'")
        self.orchestrator_state.add_category_to_workspace(workspace_id, category_name, category_description)

    def delete_category(self, workspace_id: str, category_name: str):
        """
        Delete the given category from the workspace. This call permanently deletes all data associated with the
        category, including user labels and models.
        :param workspace_id:
        :param category_name:
        """
        logging.info(f"Deleting category '{category_name}' from workspace '{workspace_id}'")
        dataset_name = self.get_dataset_name(workspace_id)
        self.data_access.delete_labels_for_category(workspace_id, dataset_name, category_name)
        self._delete_category_models(workspace_id, category_name)
        self.orchestrator_state.delete_category_from_workspace(workspace_id, category_name)

    def get_all_categories(self, workspace_id: str) -> Mapping[str, Category]:
        return self.orchestrator_state.get_workspace(workspace_id).categories

    # Data access methods

    def get_documents(self, workspace_id: str, dataset_name: str, uris: Sequence[str]) -> List[Document]:
        """
        Get a list of Documents by their URIs
        :param workspace_id:
        :param dataset_name:
        :param uris:
        :return: a list of Document objects
        """
        return self.data_access.get_documents(workspace_id, dataset_name, uris)

    def get_all_document_uris(self, workspace_id) -> List[str]:
        """
        Get a list of all document URIs in the dataset used by the given workspace.
        :param workspace_id:
        :return: a list of Document URIs
        """
        dataset_name = self.get_dataset_name(workspace_id)
        return self.data_access.get_all_document_uris(dataset_name)

    def get_all_text_elements(self, dataset_name: str) -> List[TextElement]:
        """
        Get all the text elements of the given dataset.
        :param dataset_name:
        :return: a list of TextElement objects
        """
        return self.data_access.get_all_text_elements(dataset_name=dataset_name)

    def get_text_elements_by_uris(self, workspace_id: str, dataset_name: str, uris: Sequence[str]) -> List[TextElement]:
        """
        Get a list of TextElements by their URIs
        :param workspace_id:
        :param dataset_name:
        :param uris:
        :return: a list of TextElement objects
        """
        return self.data_access.get_text_elements_by_uris(workspace_id, dataset_name, uris)

    def get_all_labeled_text_elements(self, workspace_id, dataset_name, category) -> List[TextElement]:
        """
        Get all the text elements that were assigned user labels for the given category.
        :param workspace_id:
        :param dataset_name:
        :param category:
        :return: a list of TextElement objects
        """
        return self.data_access.get_labeled_text_elements(workspace_id, dataset_name, category, sample_size=sys.maxsize,
                                                          remove_duplicates=False)['results']

    def get_all_unlabeled_text_elements(self, workspace_id, dataset_name, category) -> List[TextElement]:
        """
        Get all the text elements that were not assigned user labels for the given category.
        :param workspace_id:
        :param dataset_name:
        :param category:
        :return: a list of TextElement objects
        """
        return self.data_access.get_unlabeled_text_elements(workspace_id, dataset_name, category,
                                                            sample_size=sys.maxsize, remove_duplicates=False)['results']

    def query(self, workspace_id: str, dataset_name: str, category_name: str, query_regex: str,
              sample_size: int = sys.maxsize, sample_start_idx: int = 0, unlabeled_only: bool = False,
              remove_duplicates=False) -> Mapping[str, object]:
        """
        Query a dataset using the given regex, returning up to *sample_size* elements that meet the query
    
        :param workspace_id:
        :param dataset_name:
        :param category_name:
        :param query_regex: string
        :param unlabeled_only: if True, filters out labeled elements
        :param sample_size: maximum items to return
        :param sample_start_idx: get elements starting from this index (for pagination)
        :param remove_duplicates: if True, remove duplicate elements
        :return: a dictionary with two keys: 'results' whose value is a list of TextElements, and 'hit_count' whose
        value is the total number of TextElements in the dataset matched by the query.
        {'results': [TextElement], 'hit_count': int}
        """
        if unlabeled_only:
            return self.data_access.get_unlabeled_text_elements(workspace_id=workspace_id, dataset_name=dataset_name,
                                                                category_name=category_name, sample_size=sample_size,
                                                                sample_start_idx=sample_start_idx,
                                                                remove_duplicates=remove_duplicates)
        else:
            return self.data_access.get_text_elements(workspace_id=workspace_id, dataset_name=dataset_name,
                                                      sample_size=sample_size, sample_start_idx=sample_start_idx,
                                                      query_regex=query_regex, remove_duplicates=remove_duplicates)

    def set_labels(self, workspace_id: str, uri_to_label: Mapping[str, Mapping[str, Label]],
                   apply_to_duplicate_texts=True, update_label_counter=True):
        """
        Set user labels for a set of element URIs.
        :param workspace_id:
        :param uri_to_label: maps URIs to a dictionary in the format of {"category_name": Label}, where Label is an
        instance of data_structs.Label
        :param apply_to_duplicate_texts: if True, also set the same labels for additional URIs that are duplicates of
        the URIs provided.
        :param update_label_counter: determines whether the label changes are reflected in the label change counters
        of the categories. Since an increase in label change counts can trigger the training of a new model, in some
        specific situations this parameter is set to False and the updating of the counter is performed at a later time.
        """
        if update_label_counter:
            # count the number of labels for each category
            changes_per_cat = Counter([cat for uri, labels_dict in uri_to_label.items() for cat in labels_dict])
            for cat, num_changes in changes_per_cat.items():
                self.orchestrator_state.increase_label_change_count_since_last_train(workspace_id, cat, num_changes)
        self.data_access.set_labels(workspace_id, uri_to_label, apply_to_duplicate_texts)

    def unset_labels(self, workspace_id: str, category_name, uris: Sequence[str], apply_to_duplicate_texts=True):
        """
        Unset labels of a set of element URIs for a given category.
        :param workspace_id:
        :param category_name:
        :param uris:
        :param apply_to_duplicate_texts: if True, also unset the same labels for additional URIs that are duplicates of
        the URIs provided.
        """
        self.data_access.unset_labels(
            workspace_id, category_name, uris, apply_to_duplicate_texts=apply_to_duplicate_texts)

    def get_label_counts(self, workspace_id: str, dataset_name: str, category_name: str, remove_duplicates=False):
        """
        Get the number of elements that were labeled for the given category.
        :param workspace_id:
        :param dataset_name:
        :param category_name:
        :param remove_duplicates: whether to count all labeled elements or only unique instances
        :return:
        """
        return self.data_access.get_label_counts(workspace_id, dataset_name, category_name,
                                                 remove_duplicates=remove_duplicates)

    # Iteration-related methods

    def get_all_iterations_for_category(self, workspace_id, category_name: str) -> List[Iteration]:
        """
        :param workspace_id:
        :param category_name:
        :return: dict from model_id to ModelInfo
        """
        return self.orchestrator_state.get_all_iterations(workspace_id, category_name)

    def get_all_iterations_by_status(self, workspace_id, category_name, iteration_status: IterationStatus) \
            -> List[Iteration]:
        return self.orchestrator_state.get_all_iterations_by_status(workspace_id, category_name, iteration_status)

    def get_iteration_status(self, workspace_id, category_name, iteration_index) -> IterationStatus:
        return self.orchestrator_state.get_iteration_status(workspace_id, category_name, iteration_index)

    def delete_iteration_model(self, workspace_id, category_name, iteration_index):
        """
        Delete the model files for *iteration_index* of the given category, and mark the model as deleted.
        :param workspace_id:
        :param category_name:
        :param iteration_index:
        """
        iteration = self.get_all_iterations_for_category(workspace_id, category_name)[iteration_index]
        model_info = iteration.model
        if model_info.model_status == ModelStatus.DELETED:
            raise Exception(f"Trying to delete model id {model_info.model_id} which is already in {ModelStatus.DELETED}"
                            f"from workspace '{workspace_id}' in category '{category_name}'")

        model = self.model_factory.get_model(model_info.model_type)
        logging.info(f"Marking iteration {iteration_index} model id {model_info.model_id} in "
                     f"workspace '{workspace_id}' in category '{category_name}' as deleted, and deleting the model")
        self.orchestrator_state.mark_iteration_model_as_deleted(workspace_id, category_name, iteration_index)
        model.delete_model(model_info.model_id)

    def _delete_category_models(self, workspace_id, category_name):
        workspace = self.orchestrator_state.get_workspace(workspace_id)
        for idx in range(len(workspace.categories[category_name].iterations)):
            self.delete_iteration_model(workspace_id, category_name, idx)

    def get_elements_to_label(self, workspace_id: str, category_name: str, count: int, start_index: int = 0) \
            -> List[TextElement]:
        """
        Returns a list of *count* elements recommended for labeling by the active learning module for the latest
        iteration in READY status.
        :param workspace_id:
        :param category_name:
        :param count: maximum number of elements to return
        :param start_index: get elements starting from this index (for pagination)
        :return: a list of *count* TextElement objects
        """
        recommended_uris = self.orchestrator_state.get_current_category_recommendations(workspace_id, category_name)

        if start_index > len(recommended_uris):
            raise Exception(f"exceeded max recommended items. last element index is {len(recommended_uris) - 1}")
        recommended_uris = recommended_uris[start_index:start_index + count]
        dataset_name = self.get_dataset_name(workspace_id)
        return self.get_text_elements_by_uris(workspace_id, dataset_name, recommended_uris)

    # Iteration flow
    
    def run_iteration(self, workspace_id: str, category_name: str, model_type: ModelType, train_data,
                      train_params=None) -> str:
        """
        This method initiates an Iteration, a flow that includes training a model, inferring the full corpus using
        this model, choosing candidate elements for labeling using active learning, as well as calculating various
        statistics.
        For a specific workspace and category, an iteration is identified using an integer iteration index. As different
        stages for the given iteration are completed, the IterationStatus for this iteration index is updated using the
        self.orchestrator_state.
        Since the training and inference stages of the iteration are submitted asynchronously in the background, the
        full flow is composed of this method, along with the _train_done_callback and _infer_done_callback, which are
        launched when the training and inference stages, respectively, are completed.
    
        :param workspace_id:
        :param category_name:
        :param model_type:
        :param train_data:
        :param train_params:
        :return: model_id
        """
        def _get_counts_per_label(text_elements):
            """
            These label counts reflect the more detailed description of training labels, e.g. how many of the elements
            have weak labels
            """
            label_names = [element.category_to_label[category_name].get_detailed_label_name()
                           for element in text_elements]
    
            return dict(Counter(label_names))
    
        new_iteration_index = len(self.orchestrator_state.get_all_iterations(workspace_id, category_name))
        logging.info(f"starting iteration {new_iteration_index} in background for workspace '{workspace_id}' "
                     f"category '{category_name}' using {len(train_data)} items")
    
        train_counts = _get_counts_per_label(train_data)
        train_data = convert_text_elements_to_train_data(train_data, category_name)
        model_metadata = {TRAIN_COUNTS_STR_KEY: train_counts}
        model = self.model_factory.get_model(model_type)
    
        logging.info(f"workspace '{workspace_id}' training a model for category '{category_name}', "
                     f"model_metadata: {model_metadata}")
        model_id, _ = model.train(train_data=train_data, train_params=train_params,
                                  done_callback=functools.partial(self._train_done_callback, workspace_id,
                                                                  category_name, new_iteration_index))
        model_status = model.get_model_status(model_id)
        if train_params:
            model_metadata = {**model_metadata, **train_params}
        model_info = ModelInfo(model_id=model_id, model_status=model_status, model_type=model_type,
                               model_metadata=model_metadata, creation_date=datetime.now())
        self.orchestrator_state.add_iteration(workspace_id=workspace_id, category_name=category_name,
                                              model_info=model_info)
    
        # The model id is returned almost immediately, but the training is performed in the background. Once training is
        # complete the iteration flow continues in the *_train_done_callback* method
        return model_id

    def _train_done_callback(self, workspace_id, category_name, iteration_index, future):
        """
        Once model training for Iteration *iteration_index* is complete, the flow of the iteration continues here. As
        part of this stage an inference job over the entire dataset is launched in the background.
        :param workspace_id:
        :param category_name:
        :param iteration_index:
        :param future: future object for the train job, which was submitted through the ModelsBackgroundJobsManager
        """
        try:
            model_id = future.result()
        except Exception:
            logging.error(f"Train failed. Marking worspace '{workspace_id}' category '{category_name}' "
                          f"iteration {iteration_index} as error")
            self.orchestrator_state.update_model_status(workspace_id=workspace_id, category_name=category_name,
                                                        iteration_index=iteration_index, new_status=ModelStatus.ERROR)
            self.orchestrator_state.update_iteration_status(workspace_id, category_name, iteration_index,
                                                            IterationStatus.ERROR)
            return
    
        self.orchestrator_state.update_model_status(workspace_id=workspace_id, category_name=category_name,
                                                    iteration_index=iteration_index, new_status=ModelStatus.READY)
        self.orchestrator_state.update_iteration_status(workspace_id, category_name, iteration_index,
                                                        IterationStatus.RUNNING_INFERENCE)
        iteration = self.get_all_iterations_for_category(workspace_id, category_name)[iteration_index]
        model_info = iteration.model
        model = self.model_factory.get_model(model_info.model_type)
        dataset_name = self.get_dataset_name(workspace_id)
        elements = self.get_all_text_elements(dataset_name)
        logging.info(f"Successfully trained model id {model_id} for workspace '{workspace_id}' category "
                     f"'{category_name}' iteration {iteration_index}. Running background inference for the full "
                     f"dataset ({len(elements)} items)")
        model.infer_async(model_id, items_to_infer=[{"text": element.text} for element in elements],
                          done_callback=functools.partial(self._infer_done_callback, workspace_id, category_name,
                                                          iteration_index))
        # Inference is performed in the background. Once the infer job is complete the iteration flow continues in the
        # *_infer_done_callback* method
    
    def _infer_done_callback(self, workspace_id, category_name, iteration_index, future):
        """
        Once model inference for Iteration *iteration_index* over the full dataset is complete, the flow of the
        iteration continues here. As part of this stage the active learning module recommendations are calculated.
        :param workspace_id:
        :param category_name:
        :param iteration_index:
        :param future: future object for the inference job, which was submitted through the ModelsBackgroundJobsManager
        """
        try:
            predictions = future.result()
        except Exception:
            logging.exception(f"Background inference on workspace '{workspace_id}' category '{category_name}' "
                              f"iteration {iteration_index} Failed. Marking iteration with Error")
            self.orchestrator_state.update_iteration_status(workspace_id, category_name, iteration_index,
                                                            IterationStatus.ERROR)
            return
    
        try:
            logging.info(f"Successfully inferred all data for workspace_id '{workspace_id}'"
                         f" category '{category_name}' iteration {iteration_index}, "
                         f"calculating statistics and updating active learning recommendations")
    
            self._calculate_iteration_statistics(workspace_id, category_name, iteration_index, predictions)
    
            self.orchestrator_state.update_iteration_status(workspace_id, category_name,
                                                            iteration_index, IterationStatus.RUNNING_ACTIVE_LEARNING)
            dataset_name = self.get_dataset_name(workspace_id)
            self._calculate_active_learning_recommendations(workspace_id, dataset_name, category_name,
                                                            ACTIVE_LEARNING_SUGGESTION_COUNT, iteration_index)
            self.orchestrator_state.update_iteration_status(workspace_id, category_name, iteration_index,
                                                            IterationStatus.READY)
            logging.info(f"Successfully finished iteration {iteration_index} "
                         f"in workspace '{workspace_id}' category '{category_name}'.")
    
        except Exception:
            logging.exception(f"Iteration {iteration_index} on workspace '{workspace_id}' category '{category_name}' "
                              f"Failed. Marking iteration with Error")
            self.orchestrator_state.update_iteration_status(workspace_id, category_name, iteration_index,
                                                            IterationStatus.ERROR)
        try:
            self._delete_old_models(workspace_id, category_name, iteration_index)
        except Exception:
            logging.exception(f"Failed to delete old models for workspace '{workspace_id}' category '{category_name}' "
                              f"after iteration {iteration_index} finished successfully ")

    def _calculate_iteration_statistics(self, workspace_id, category_name, iteration_index,
                                        predictions: Sequence[Prediction]):
        """
        Calculate some statistics about the *iteration_index* model and store them in the workspace
        :param workspace_id:
        :param category_name:
        :param iteration_index:
        :param predictions: model predictions of the *iteration_index* model over the entire dataset
        """
        dataset_name = self.get_dataset_name(workspace_id)
        elements = self.get_all_text_elements(dataset_name)
        dataset_size = len(predictions)
    
        # calculate the fraction of examples that receive a positive prediction from the current model
        positive_fraction = sum([pred.label is True for pred in predictions])/dataset_size
        post_train_statistics = {"positive_fraction": positive_fraction}
    
        # calculate the fraction of predictions that changed between the previous model and the current model
        previous_iterations = self.orchestrator_state.get_all_iterations(workspace_id, category_name)[:iteration_index]
        previous_ready_iteration_indices = \
            [candidate_iteration_index for candidate_iteration_index, iteration in enumerate(previous_iterations)
             if iteration.status == IterationStatus.READY]
        if len(previous_ready_iteration_indices) > 0:
            previous_model_predictions = self.infer(workspace_id, category_name, elements,
                                                    iteration_index=previous_ready_iteration_indices[-1])
            num_identical = sum(x.label == y.label for x, y in zip(predictions, previous_model_predictions))
            post_train_statistics["changed_fraction"] = (dataset_size-num_identical)/dataset_size
    
        logging.info(f"post train measurements for iteration {iteration_index}: {post_train_statistics}")
        self.orchestrator_state.add_iteration_statistics(workspace_id, category_name, iteration_index,
                                                         post_train_statistics)

    def _calculate_active_learning_recommendations(self, workspace_id, dataset_name, category_name, count,
                                                   iteration_index: int):
        """
        Calculate the next recommended elements for labeling using the AL module and store them in the workspace
        :param workspace_id:
        :param dataset_name:
        :param category_name:
        :param count:
        :param iteration_index: iteration to use
        """
        active_learner = self.active_learning_factory.get_active_learner(self.config.active_learning_strategy)
        unlabeled = self.get_all_unlabeled_text_elements(workspace_id, dataset_name, category_name)
        predictions = self.infer(workspace_id, category_name, unlabeled, iteration_index)
        new_recommendations = active_learner.get_recommended_items_for_labeling(
            workspace_id=workspace_id, dataset_name=dataset_name, category_name=category_name,
            candidate_text_elements=unlabeled, candidate_text_element_predictions=predictions, sample_size=count)
        self.orchestrator_state.update_category_recommendations(workspace_id=workspace_id, category_name=category_name,
                                                                iteration_index=iteration_index,
                                                                recommended_items=[x.uri for x in new_recommendations])
    
        logging.info(f"active learning recommendations for iteration index {iteration_index} "
                     f"of category '{category_name}' are ready")

    def _delete_old_models(self, workspace_id, category_name, iteration_index):
        """
        Delete previous model files for a given workspace and category, keeping only the latest
        *NUMBER_OF_MODELS_TO_KEEP* models for which an iteration flow has completed successfully.
        :param workspace_id:
        :param category_name:
        :param iteration_index:
        """
        iterations = self.orchestrator_state.get_all_iterations(workspace_id, category_name)[:iteration_index+1]
        ready_iteration_indices = \
            [candidate_iteration_index for candidate_iteration_index, iteration in enumerate(iterations)
             if iteration.status == IterationStatus.READY]
    
        for candidate_iteration_index in ready_iteration_indices[:-NUMBER_OF_MODELS_TO_KEEP]:
            logging.info(f"keep only {NUMBER_OF_MODELS_TO_KEEP} models, deleting iteration {candidate_iteration_index}")
            self.delete_iteration_model(workspace_id, category_name, candidate_iteration_index)

    def train_if_recommended(self, workspace_id: str, category_name: str, force=False) -> Union[None, str]:
        """
        Check if the minimal threshold for training a new model has been met, and if so, start the flow of a
        new Iteration.
        :param workspace_id:
        :param category_name:
        :param force: if True, launch an iteration regardless of whether the thresholds are met
        :return: None if no training was launched, else the model_id for the training job submitted in the background
        """
        try:
            workspace = self.orchestrator_state.get_workspace(workspace_id)
            dataset_name = workspace.dataset_name
    
            iterations = workspace.categories[category_name].iterations
            iterations_without_errors = [iteration for iteration in iterations
                                         if iteration.status != IterationStatus.ERROR]
    
            label_counts = self.data_access.get_label_counts(workspace_id=workspace_id, dataset_name=dataset_name,
                                                             category_name=category_name, remove_duplicates=True)
            changes_since_last_model = \
                self.orchestrator_state.get_label_change_count_since_last_train(workspace_id, category_name)
            if force or (LABEL_POSITIVE in label_counts
                         and label_counts[LABEL_POSITIVE] >= self.config.first_model_positive_threshold
                         and changes_since_last_model >= self.config.changed_element_threshold):
                if len(iterations_without_errors) > 0 and iterations_without_errors[-1].status != IterationStatus.READY:
                    logging.info(f"workspace '{workspace_id}' category '{category_name}' new elements criterion was "
                                 f"met but previous AL not yet ready, not initiating a new training")
                    return None
                self.orchestrator_state.set_label_change_count_since_last_train(workspace_id, category_name, 0)
                logging.info(
                    f"Workspace '{workspace_id}' category '{category_name}' "
                    f"{label_counts[LABEL_POSITIVE]} positive elements (>={self.config.first_model_positive_threshold})"
                    f" {changes_since_last_model} elements changed since last model "
                    f"(>={self.config.changed_element_threshold}). Training a new model")
                iteration_num = len(iterations_without_errors)
                model_type = self.config.model_policy.get_model_type(iteration_num)
                train_set_selector = get_training_set_selector(self.data_access,
                                                               strategy=self.config.training_set_selection_strategy)
                train_data = train_set_selector.get_train_set(workspace_id=workspace_id,
                                                              train_dataset_name=dataset_name,
                                                              category_name=category_name)
                model_id = self.run_iteration(workspace_id=workspace_id, category_name=category_name,
                                              model_type=model_type, train_data=train_data)
                return model_id
            else:
                logging.info(f"{label_counts[LABEL_POSITIVE]} positive elements "
                             f"(should be >={self.config.first_model_positive_threshold}) "
                             f"AND {changes_since_last_model} elements changed since last model "
                             f"(should be >={self.config.changed_element_threshold}). not training a new model")
                return None
        except Exception:
            logging.exception("train_if_recommended failed in a background thread. Model will not be trained")

    def infer(self, workspace_id: str, category_name: str, elements_to_infer: Sequence[TextElement],
              iteration_index: int = None, use_cache: bool = True) -> Sequence[Prediction]:
        """
        Get the model predictions for a list of TextElements
        :param workspace_id:
        :param category_name:
        :param elements_to_infer: list of TextElements
        :param iteration_index: iteration to use. If set to None, the latest model for the category will be used
        :param use_cache: utilize a cache that stores inference results
        :return: a list of Prediction objects
        """
        if len(elements_to_infer) == 0:
            return []
    
        iterations = self.get_all_iterations_for_category(workspace_id, category_name)
        if iteration_index is None:  # use latest ready model
            iteration = [it for it in iterations if it.status == IterationStatus.READY][-1]
        else:
            iteration = iterations[iteration_index]
            if iteration.status in [IterationStatus.TRAINING, IterationStatus.MODEL_DELETED, IterationStatus.ERROR]:
                raise Exception(f"iteration {iteration_index} in workspace '{workspace_id}' "
                                f"category '{category_name}' is not ready for inference. "
                                f"(current status is {iteration.status}, model status is {iteration.model.model_status})")
    
        model_info = iteration.model
        if model_info.model_status != ModelStatus.READY:
            raise Exception(f"model id {model_info.model_id} is not in READY status"
                            f"while iteration status is {iteration.status}.  Something went wrong")
    
        model = self.model_factory.get_model(model_info.model_type)
        list_of_dicts = [{"text": element.text} for element in elements_to_infer]
        predictions = model.infer(model_id=model_info.model_id, items_to_infer=list_of_dicts, use_cache=use_cache)
        return predictions
    
    # Labeling/Evaluation reports

    def get_contradiction_report(self, workspace_id, category_name) -> Mapping[str, List]:
        dataset_name = self.get_dataset_name(workspace_id)
        labeled_elements = self.get_all_labeled_text_elements(workspace_id, dataset_name, category_name)
        return get_suspected_labeling_contradictions_by_distance_with_diffs(category_name, labeled_elements)

    def get_suspicious_elements_report(self, workspace_id, category_name,
                                       model_type: ModelType = ModelsCatalog.SVM_ENSEMBLE) -> List[TextElement]:
        dataset_name = self.get_dataset_name(workspace_id)
        labeled_elements = self.get_all_labeled_text_elements(workspace_id, dataset_name, category_name)
        return get_disagreements_using_cross_validation(workspace_id, category_name, labeled_elements,
                                                        self.model_factory, model_type)

    def estimate_precision(self, workspace_id, category, ids, changed_elements_count, iteration_index):
        dataset_name = self.get_dataset_name(workspace_id)
        text_elements = self.get_text_elements_by_uris(workspace_id, dataset_name, ids)
        positive_elements = [te for te in text_elements if te.category_to_label[category].label == LABEL_POSITIVE]
    
        estimated_precision = len(positive_elements)/len(text_elements)
        self.orchestrator_state.add_iteration_statistics(workspace_id, category, iteration_index,
                                                         {"estimated_precision": estimated_precision,
                                                          "estimated_precision_num_elements": len(ids)})
    
        # since we don't want a new model to train while labeling in precision evaluation mode, we only update the
        # labeling counts after evaluation is finished
        self.orchestrator_state.increase_label_change_count_since_last_train(workspace_id, category,
                                                                             changed_elements_count)
        return estimated_precision

    def sample_elements_by_prediction(self, workspace_id, category, sample_size: int = sys.maxsize,
                                      unlabeled_only=False, required_label=LABEL_POSITIVE, random_state: int = 0):
        dataset_name = self.get_dataset_name(workspace_id)
        if unlabeled_only:
            elements = self.get_all_unlabeled_text_elements(workspace_id, dataset_name, category)
        else:
            elements = self.get_all_text_elements(dataset_name)
        predictions = self.infer(workspace_id, category, elements)
        elements_with_matching_prediction = [text_element for text_element, prediction in zip(elements, predictions)
                                             if prediction.label == required_label]
        random.Random(random_state).shuffle(elements_with_matching_prediction)
        return elements_with_matching_prediction[:sample_size]

    def get_progress(self, workspace_id: str, dataset_name: str, category: str):
        category_label_counts = self.get_label_counts(workspace_id, dataset_name, category)
        if category_label_counts[LABEL_POSITIVE]:
            changed_since_last_model_count = \
                self.orchestrator_state.get_label_change_count_since_last_train(workspace_id, category)

            return {"all": min(
                max(0, min(round(changed_since_last_model_count / self.config.changed_element_threshold * 100), 100)),
                max(0, min(round(category_label_counts[LABEL_POSITIVE] /
                                 self.config.first_model_positive_threshold * 100), 100)))
            }
        else:
            return {"all": 0}

    # Import/Export

    def import_category_labels(self, workspace_id, labels_df_to_import: pd.DataFrame):
        logging.info(f"Importing {len(labels_df_to_import)} unique labeled elements into workspace '{workspace_id}'"
                     f"from {len(labels_df_to_import[DisplayFields.category_name].unique())} categories")
        dataset_name = self.get_dataset_name(workspace_id)
        imported_categories_to_uris_and_labels = process_labels_dataframe(workspace_id, dataset_name, self.data_access,
                                                                          labels_df_to_import)
    
        existing_categories = self.get_all_categories(workspace_id)
        categories_counter = defaultdict(int)
        categories_created = []
        lines_skipped = []
        for category_name, uri_to_label in imported_categories_to_uris_and_labels.items():
            if category_name not in existing_categories:
                logging.info(f"** category '{category_name}' is missing, creating it ** ")
                self.create_new_category(workspace_id, category_name, f'{category_name} (created during upload)')
                categories_created.append(category_name)
    
            logging.info(f'{category_name}: adding labels for {len(uri_to_label)} uris')
            self.set_labels(workspace_id, uri_to_label,
                            apply_to_duplicate_texts=self.config.apply_labels_to_duplicate_texts,
                            update_label_counter=True)
    
            label_counts_dict = self.get_label_counts(workspace_id, dataset_name, category_name, False)
            logging.info(f"Updated total label count in workspace '{workspace_id}' for category {category_name} "
                         f"is {sum(label_counts_dict.values())} ({label_counts_dict})")
            categories_counter[category_name] = len(uri_to_label)
        # TODO return both positive and negative counts
        categories_counter_list = [{'category': key, 'counter': value} for key, value in categories_counter.items()]
        total = sum(categories_counter.values())
    
        res = {'categories': categories_counter_list,
               'categoriesCreated': categories_created,
               'linesSkipped': lines_skipped,
               'total': total}
        return res
    
    def export_workspace_labels(self, workspace_id) -> pd.DataFrame:
        dataset_name = self.get_dataset_name(workspace_id)
        categories = self.get_all_categories(workspace_id)
        list_of_dicts = []
        for category in categories:
            label_count = sum(self.get_label_counts(workspace_id, dataset_name, category, False).values())
            logging.info(f"Total label count in workspace '{workspace_id}' is {label_count}")
            labeled_elements = self.data_access.get_labeled_text_elements(workspace_id, dataset_name, category,
                                                                          remove_duplicates=False)['results']
            logging.info(f"Exporting {len(labeled_elements)} unique labeled elements for category '{category}'"
                         f" from workspace '{workspace_id}'")
            list_of_dicts.extend(
                [{DisplayFields.workspace_id: workspace_id,
                  DisplayFields.category_name: category,
                  DisplayFields.doc_id: le.uri.split('-')[1],  # TODO handle when handling uri/doc_id/element_id
                  DisplayFields.dataset: dataset_name,
                  DisplayFields.text: le.text,
                  DisplayFields.uri: le.uri,
                  DisplayFields.element_metadata: le.metadata,
                  DisplayFields.label: le.category_to_label[category].label,
                  DisplayFields.label_metadata: le.category_to_label[category].metadata,
                  DisplayFields.label_type: le.category_to_label[category].label_type.name}
                 for le in labeled_elements])
        return pd.DataFrame(list_of_dicts)

    def export_model(self, workspace_id, category_name, iteration_index):
        iteration = self.orchestrator_state.get_all_iterations(workspace_id, category_name)[iteration_index]
    
        model = self.model_factory.get_model(iteration.model.model_type)
        exported_model_dir = model.export_model(iteration.model.model_id)
        return exported_model_dir
    
    def add_documents_from_file(self, dataset_name, temp_file_path):
        global new_data_infer_thread_pool
        logging.info(f"adding documents to dataset '{dataset_name}'")
        documents = CsvFileProcessor(dataset_name, temp_file_path).build_documents()
        document_statistics = self.data_access.add_documents(dataset_name, documents)
        workspaces_to_update = []
        total_infer_jobs = 0
        for workspace_id in self.list_workspaces():
            workspace = self.orchestrator_state.get_workspace(workspace_id)
            if workspace.dataset_name == dataset_name:
                workspaces_to_update.append(workspace_id)
    
                for category_name, category in workspace.categories.items():
                    if self.config.apply_labels_to_duplicate_texts:
                        # since new data may contain texts identical to existing labeled texts, we set all the existing
                        # labels again to apply the labels to the new data
                        labeled_elements = self.data_access.get_labeled_text_elements(workspace_id, dataset_name,
                                                                                      category_name)['results']
                        uri_to_label = {te.uri: te.category_to_label for te in labeled_elements}
                        self.data_access.set_labels(workspace_id, uri_to_label, apply_to_duplicate_texts=True)
    
                    if len(category.iterations) > 0:
                        iteration_index = len(category.iterations)-1
                        new_data_infer_thread_pool.submit(self._infer_missing_elements, workspace_id, category,
                                                          dataset_name, iteration_index)
                        total_infer_jobs += 1
        logging.info(f"done adding documents to {dataset_name} upload statistics: {document_statistics}."
                     f"{total_infer_jobs} infer jobs were submitted in the background")
        return document_statistics, workspaces_to_update
    
    def _infer_missing_elements(self, workspace_id, category, dataset_name, iteration_index):
        iteration_status = self.get_iteration_status(workspace_id, category, iteration_index)
        if iteration_status == IterationStatus.ERROR:
            logging.error(
                f"Cannot run inference for category '{category}' in workspace '{workspace_id}' after new documents were"
                f" loaded to dataset '{dataset_name}' using model {iteration_index}, as the iteration status is ERROR")
            return
        # if *model_id* is currently training, wait for training to complete
        start_time = time.time()
        while iteration_status != IterationStatus.READY:
            wait_time = time.time() - start_time
            if wait_time > 15 * 60:
                logging.error(f"Timeout reached when waiting to run inference with the last model for category "
                              f"'{category}' in workspace '{workspace_id}' after new documents were loaded to "
                              f"dataset '{dataset_name}' using model {iteration_index}")
                return
            logging.info(f"waiting for iteration {iteration_index} to complete in order to infer newly added documents"
                         f" for category '{category}' in workspace '{workspace_id}'")
            time.sleep(30)
            iteration_status = self.get_iteration_status(workspace_id, category, iteration_index)
    
        logging.info(f"Running inference with the latest model for category '{category}' in workspace '{workspace_id}' "
                     f"after new documents were loaded to dataset '{dataset_name}' using model {iteration_index}")
        # Currently, there is no indication to the user that inference is running on the new documents, and there is no
        # indication for when this inferences ends. Can be added and reflected in the UI in the future
        self.infer(workspace_id, category, self.get_all_text_elements(dataset_name), iteration_index)
        logging.info(f"completed inference with the latest model for category '{category}' in workspace "
                     f"'{workspace_id}' after new documents were loaded to dataset '{dataset_name}',"
                     f"using model {iteration_index}")