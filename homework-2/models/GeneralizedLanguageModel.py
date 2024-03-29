from components import Helper
import numpy as np
import collections
import itertools
import gensim
import pyndri
import pickle
import time
import math
import os


class GeneralizedLanguageModel:
    """Generalized language model.

    Notes:
        The model uses a generative process that allows for a noisy channel transformation of term given it's context.
        The observation of a term is modelled from 3 independent events: direct term sampling, transformation via
        document sampling, transformation via collection sampling.

        The implementation pre-computes all document similarity sums and pickles them. The similarities are computed for
        all documents in the inverted index.

        The output of the scoring function is the log-likelihood of the query given the document.

        IMPORTANT: Only works for a subset document collection. THe run method must receive the same collection that is
        passed on model initialisation.

    Attributes:
        index: pyndry index for the entire collection.
        inverted_index: dict of term frequencies per document.
        doc_len: dict of documents lengths.
        col_freq: dict of term frequencies for the entire collection.
        col_size: size of the document collection (number of query terms in the collection)
        lamb: parameter to control weight of direct term sampling. (lambda)
        alph: parameter to control weight of document sampling transformation. (alpha)
        beta: parameter to control weight of collection sampling transformation. (beta)
    """

    def __init__(self, index: pyndri.Index, inverted_index: collections.defaultdict(dict),
                 queries: dict, col_freq: collections.defaultdict(int), doc_len: dict, lamb: float, alph: float,
                 beta: float, doc_col: dict = None):
        """Initialize positional language model.

        Args:
            index: pyndry index for the entire collection.
            inverted_index: dict of term frequencies per document.
            queries: dict of queries
            col_freq: dict of term frequencies for the entire collection.
            doc_len: dict of documents lengths.
            lamb: parameter to control weight of direct term sampling. (lambda)
            alph: parameter to control weight of document sampling transformation. (alpha)
            beta: parameter to control weight of collection sampling transformation. (beta)
            doc_col: dict of tfidf top docs per query.
        """
        self.index = index
        self.inverted_index = inverted_index
        self.queries = queries
        self.col_freq = col_freq
        self.col_size = sum(col_freq)
        self.doc_len = doc_len
        self.lamb = lamb
        self.alph = alph
        self.beta = beta
        self.doc_col = doc_col

        self.token2id, self.id2token, self.id2df = self.index.get_dictionary()
        self.word2vec = gensim.models.Word2Vec.load('../word2vec/Word2Vec')

        try:
            with open('../pickles/doc_sim_sums.pkl', 'rb') as file:
                self.doc_sim_sum = pickle.load(file)
        except FileNotFoundError:
            self.doc_sim_sum = self.compute_doc_sim_sum()
            with open('../pickles/doc_sim_sums.pkl', 'wb') as file:
                pickle.dump(dict(self.doc_sim_sum), file)

        try:
            with open('../pickles/col_Nt.pkl', 'rb') as file:
                self.col_Nt, self.sum_nT = pickle.load(file)
        except FileNotFoundError:
            self.col_Nt, self.sum_nT = self.compute_col_nt(3)
            with open('../pickles/col_Nt.pkl', 'wb') as file:
                pickle.dump((self.col_Nt, self.sum_nT), file)

    def score(self, int_doc_id: int, query: tuple) -> float:
        """Compute the score for a document and a query.

        Args:
            int_doc_id: the document id.
            query: tuple of query term ids.
        Return:
            Maximum position-based score of a query given a document.
        """

        # noinspection PyArgumentList
        ext_doc_id, doc = self.index.document(int_doc_id)
        score = 0
        for query_term_id in query:
            score += math.log(self.lamb * self.compute_term_likelihood(query_term_id, int_doc_id) +
                              self.alph * self.compute_doc_transform(query_term_id, int_doc_id, doc) +
                              self.beta * self.compute_col_transform(query_term_id) +
                              (1 - self.lamb - self.alph - self.beta) * self.compute_bg_likelihood(query_term_id))

        return score

    def compute_term_likelihood(self, query_term_id: int, int_doc_id: int) -> float:
        """ Compute likelihood of term given document.

        Args:
            query_term_id: id of query term.
            int_doc_id: internal document id.

        Returns:
            Query term likelihood given document.
        """
        doc_term_freq = self.inverted_index[query_term_id].get(int_doc_id, 0)
        doc_len = self.doc_len[int_doc_id]
        return doc_term_freq / doc_len

    def compute_bg_likelihood(self, query_term_id):
        """Compute background probability of a query term.

        Args:
            query_term_id: id of query term.

        Returns:
            Query term likelihood given collection.
        """
        col_term_freq = self.col_freq[query_term_id]
        return col_term_freq / self.col_size

    def compute_doc_transform(self, query_term_id: int, int_doc_id: int, doc: tuple) -> np.ndarray:
        """Compute document noisy channel transform.

        Args:
            query_term_id: id of query term.
            int_doc_id: document internal id.
            doc: tuple of document terms.
        Returns:
            Probability under document transform.
        """
        query_vec = self.word2vec.wv[self.id2token[query_term_id]].reshape(-1, 1)

        doc_arr = np.array(doc)
        filter_doc_arr = doc_arr[doc_arr != 0]
        filter_doc_arr_len = len(filter_doc_arr)
        doc_model = np.full((filter_doc_arr_len, 300), np.nan)
        term_frequencies = np.full((filter_doc_arr_len, 1), np.nan)
        for idx, term_id in enumerate(filter_doc_arr):
            doc_model[idx, :] = self.word2vec.wv[self.id2token[term_id]]
            term_frequencies[idx, 0] = np.sum(filter_doc_arr == term_id)
        dot_prod = np.dot(doc_model, query_vec)
        norms = np.linalg.norm(doc_model, axis=1).reshape(-1, 1)
        similarities = dot_prod / norms / np.linalg.norm(query_vec)
        doc_transform = np.sum(
            similarities * term_frequencies / (self.doc_sim_sum[int_doc_id] * self.doc_len[int_doc_id]))

        return doc_transform

    def compute_col_transform(self, query_term_id: int) -> float:
        """Compute collection noisy channel transform

        Args:
            query_term_id: id of query term.

        Returns:
            Probability of collection transform.
        """
        col_transform = 0
        query_term = self.id2token[query_term_id]
        query_vec = self.word2vec.wv[query_term]

        for neighbour in self.col_Nt[query_term]:
            similarity = self.cos_sim(query_vec, self.word2vec.wv[neighbour])

            col_transform += similarity / self.sum_nT[query_term] * self.col_freq[
                self.token2id[neighbour]] / self.col_size

        return col_transform

    def compute_doc_sim_sum(self):
        """Compute document term similarity sums.

        Returns:
            dict of similarity sums for each document.
        """

        doc_sim_sums = collections.defaultdict(lambda: 0)
        s = time.time()
        unique_ids = np.unique(list(itertools.chain.from_iterable(self.doc_col.values())))

        for int_doc_id in unique_ids:
            # noinspection PyArgumentList
            ext_doc_id, doc = self.index.document(int_doc_id)
            doc_arr = np.array(doc)
            filter_doc_arr = doc_arr[doc_arr != 0]
            filter_doc_arr_len = len(filter_doc_arr)
            doc_model = np.full((filter_doc_arr_len, 300), np.nan)
            i2t = dict()
            t2i = dict()
            for idx, term_id in enumerate(filter_doc_arr):
                i2t[idx] = term_id
                t2i[term_id] = idx
                doc_model[idx, :] = self.word2vec.wv[self.id2token[term_id]]
            norms = np.linalg.norm(doc_model, axis=1).reshape(-1, 1)
            cos_sim = doc_model.dot(doc_model.T) / norms / norms.T
            upper_indices = np.triu_indices(filter_doc_arr_len - 1, 1)
            doc_sim_sums[int_doc_id] = np.sum(cos_sim[1:, 1:][upper_indices])

        print(time.time() - s)
        return dict(doc_sim_sums)

    def compute_col_nt(self, n_terms=3):
        """Retrieve first N term neighbours

        Args:
            n_terms: context size.

        Returns:
            dict of term neighbours.
        """
        term_neighbours = dict()
        term_neighbours_sum = dict()
        for query_id, int_doc_ids in self.doc_col.items():
            query = self.queries[query_id]

            for query_term_id in query:
                query_term = self.id2token[query_term_id]
                term_neighbours[query_term] = list(
                    map(lambda x: x[0], self.word2vec.most_similar(query_term, topn=n_terms)))
                term_neighbours_sum[query_term] = sum(
                    [self.cos_sim(self.word2vec.wv[query_term], self.word2vec.wv[ngh_term]) for ngh_term in
                     term_neighbours[query_term]])

        return dict(term_neighbours), dict(term_neighbours_sum)

    # noinspection PyMethodMayBeStatic
    def cos_sim(self, v1: np.ndarray, v2: np.ndarray) -> float:
        """Calculate cosine similarity between two vectors."""
        return float(np.dot(v1, v2) / (np.linalg.norm(v1) * np.linalg.norm(v2)))

    def run(self, model_name: str, doc_col=None) -> collections.defaultdict or None:
        """
        Runs a retrieval method for all the queries and writes the TREC-friendly results in a file.
        Optionally, accepts a collection of documents to re-rank for each query.
        Args:
            model_name: the name of the model (a string)
            doc_col: pass a preprocessed query-wise collection fo documents to be evaluated.
        Returns:
            Data written to a run file.
        """

        run_out_path = '../retrievals/{}.run'.format(model_name)

        if os.path.exists(run_out_path):
            print('Run file already exists. Aborting.')
            return

        data = collections.defaultdict(list)
        scores = collections.defaultdict(lambda: collections.defaultdict(lambda: 0))

        print('Retrieving using', model_name)
        retrieval_start_time = time.time()

        if doc_col is None:
            for int_doc_id in range(self.index.document_base(), self.index.maximum_document()):
                # noinspection PyArgumentList
                ext_doc_id, doc = self.index.document(int_doc_id)

                for query_id, query in self.queries.items():
                    doc_is_in_inverted_index = False
                    for query_term_id in query:
                        doc_term_freq = self.inverted_index[query_term_id].get(int_doc_id)
                        if doc_term_freq is not None:
                            doc_is_in_inverted_index = True
                    if doc_is_in_inverted_index:
                        scores[query_id][ext_doc_id] = self.score(int_doc_id, query)

                    if scores[query_id][ext_doc_id] != 0:
                        data[query_id].append((scores[query_id][ext_doc_id], ext_doc_id))
        else:
            for query_id, int_doc_ids in doc_col.items():
                query_start_time = time.time()
                query = self.queries[query_id]
                for int_doc_id in int_doc_ids:
                    # noinspection PyArgumentList
                    ext_doc_id, doc = self.index.document(int_doc_id)
                    doc_is_in_inverted_index = False
                    for query_term_id in query:
                        doc_term_freq = self.inverted_index[query_term_id].get(int_doc_id)
                        if doc_term_freq is not None:
                            doc_is_in_inverted_index = True

                    if doc_is_in_inverted_index:
                        scores[query_id][ext_doc_id] = self.score(int_doc_id, query)

                    if scores[query_id][ext_doc_id] != 0:
                        data[query_id].append((scores[query_id][ext_doc_id], ext_doc_id))
                print('Query {} took {}'.format(query_id, time.time() - query_start_time))
        with open(run_out_path, 'w') as f_out:
            Helper.write_run(
                model_name=model_name,
                data=data,
                out_f=f_out,
                max_objects_per_query=1000)

        print('Retrieval run took {} seconds.'.format(time.time() - retrieval_start_time))
        return data


if __name__ == '__main__':
    with open('../pickles/prepro_doc_col_q50_top1000_tfidf.pkl', 'rb') as file:
        doc_col = pickle.load(file)
        generalizedLanguageModel = GeneralizedLanguageModel(index=Helper.index, inverted_index=Helper.inverted_index,
                                                            queries=Helper.tokenized_queries,
                                                            col_freq=Helper.collection_frequencies,
                                                            doc_len=Helper.document_lengths,
                                                            lamb=0.33, alph=0.33, beta=0.33, doc_col=doc_col)
        generalizedLanguageModel.run('GLM_top1000docs_sigma50_mu1000', doc_col)
