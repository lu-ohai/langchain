from __future__ import annotations

import logging
from abc import abstractmethod
from collections.abc import AsyncIterable, Collection, Iterable, Iterator
from typing import (
    Any,
    ClassVar,
    Optional,
    Sequence,
    cast,
)

from langchain_core._api import beta
from langchain_core.callbacks import (
    AsyncCallbackManagerForRetrieverRun,
    CallbackManagerForRetrieverRun,
)
from langchain_core.documents import Document
from langchain_core.load import Serializable
from langchain_core.runnables import run_in_executor
from langchain_core.vectorstores import VectorStore, VectorStoreRetriever
from pydantic import Field

from langchain_community.graph_vectorstores.links import METADATA_LINKS_KEY, Link

logger = logging.getLogger(__name__)


def _has_next(iterator: Iterator) -> bool:
    """Checks if the iterator has more elements.
    Warning: consumes an element from the iterator"""
    sentinel = object()
    return next(iterator, sentinel) is not sentinel


@beta()
class Node(Serializable):
    """Node in the GraphVectorStore.

    Edges exist from nodes with an outgoing link to nodes with a matching incoming link.

    For instance two nodes `a` and `b` connected over a hyperlink ``https://some-url``
    would look like:

    .. code-block:: python

        [
            Node(
                id="a",
                text="some text a",
                links= [
                    Link(kind="hyperlink", tag="https://some-url", direction="incoming")
                ],
            ),
            Node(
                id="b",
                text="some text b",
                links= [
                    Link(kind="hyperlink", tag="https://some-url", direction="outgoing")
                ],
            )
        ]
    """

    id: Optional[str] = None
    """Unique ID for the node. Will be generated by the GraphVectorStore if not set."""
    text: str
    """Text contained by the node."""
    metadata: dict = Field(default_factory=dict)
    """Metadata for the node."""
    links: list[Link] = Field(default_factory=list)
    """Links associated with the node."""


def _texts_to_nodes(
    texts: Iterable[str],
    metadatas: Optional[Iterable[dict]],
    ids: Optional[Iterable[str]],
) -> Iterator[Node]:
    metadatas_it = iter(metadatas) if metadatas else None
    ids_it = iter(ids) if ids else None
    for text in texts:
        try:
            _metadata = next(metadatas_it).copy() if metadatas_it else {}
        except StopIteration as e:
            raise ValueError("texts iterable longer than metadatas") from e
        try:
            _id = next(ids_it) if ids_it else None
        except StopIteration as e:
            raise ValueError("texts iterable longer than ids") from e

        links = _metadata.pop(METADATA_LINKS_KEY, [])
        if not isinstance(links, list):
            links = list(links)
        yield Node(
            id=_id,
            metadata=_metadata,
            text=text,
            links=links,
        )
    if ids_it and _has_next(ids_it):
        raise ValueError("ids iterable longer than texts")
    if metadatas_it and _has_next(metadatas_it):
        raise ValueError("metadatas iterable longer than texts")


def _documents_to_nodes(documents: Iterable[Document]) -> Iterator[Node]:
    for doc in documents:
        metadata = doc.metadata.copy()
        links = metadata.pop(METADATA_LINKS_KEY, [])
        if not isinstance(links, list):
            links = list(links)
        yield Node(
            id=doc.id,
            metadata=metadata,
            text=doc.page_content,
            links=links,
        )


@beta()
def nodes_to_documents(nodes: Iterable[Node]) -> Iterator[Document]:
    """Convert nodes to documents.

    Args:
        nodes: The nodes to convert to documents.
    Returns:
        The documents generated from the nodes.
    """
    for node in nodes:
        metadata = node.metadata.copy()
        metadata[METADATA_LINKS_KEY] = [
            # Convert the core `Link` (from the node) back to the local `Link`.
            Link(kind=link.kind, direction=link.direction, tag=link.tag)
            for link in node.links
        ]

        yield Document(
            id=node.id,
            page_content=node.text,
            metadata=metadata,
        )


@beta(message="Added in version 0.3.1 of langchain_community. API subject to change.")
class GraphVectorStore(VectorStore):
    """A hybrid vector-and-graph graph store.

    Document chunks support vector-similarity search as well as edges linking
    chunks based on structural and semantic properties.

    .. versionadded:: 0.3.1
    """

    @abstractmethod
    def add_nodes(
        self,
        nodes: Iterable[Node],
        **kwargs: Any,
    ) -> Iterable[str]:
        """Add nodes to the graph store.

        Args:
            nodes: the nodes to add.
            **kwargs: Additional keyword arguments.
        """

    async def aadd_nodes(
        self,
        nodes: Iterable[Node],
        **kwargs: Any,
    ) -> AsyncIterable[str]:
        """Add nodes to the graph store.

        Args:
            nodes: the nodes to add.
            **kwargs: Additional keyword arguments.
        """
        iterator = iter(await run_in_executor(None, self.add_nodes, nodes, **kwargs))
        done = object()
        while True:
            doc = await run_in_executor(None, next, iterator, done)
            if doc is done:
                break
            yield doc  # type: ignore[misc]

    def add_texts(
        self,
        texts: Iterable[str],
        metadatas: Optional[Iterable[dict]] = None,
        *,
        ids: Optional[Iterable[str]] = None,
        **kwargs: Any,
    ) -> list[str]:
        """Run more texts through the embeddings and add to the vector store.

        The Links present in the metadata field `links` will be extracted to create
        the `Node` links.

        Eg if nodes `a` and `b` are connected over a hyperlink `https://some-url`, the
        function call would look like:

        .. code-block:: python

            store.add_texts(
                ids=["a", "b"],
                texts=["some text a", "some text b"],
                metadatas=[
                    {
                        "links": [
                            Link.incoming(kind="hyperlink", tag="https://some-url")
                        ]
                    },
                    {
                        "links": [
                            Link.outgoing(kind="hyperlink", tag="https://some-url")
                        ]
                    },
                ],
            )

        Args:
            texts: Iterable of strings to add to the vector store.
            metadatas: Optional list of metadatas associated with the texts.
                The metadata key `links` shall be an iterable of
                :py:class:`~langchain_community.graph_vectorstores.links.Link`.
            ids: Optional list of IDs associated with the texts.
            **kwargs: vector store specific parameters.

        Returns:
            List of ids from adding the texts into the vector store.
        """
        nodes = _texts_to_nodes(texts, metadatas, ids)
        return list(self.add_nodes(nodes, **kwargs))

    async def aadd_texts(
        self,
        texts: Iterable[str],
        metadatas: Optional[Iterable[dict]] = None,
        *,
        ids: Optional[Iterable[str]] = None,
        **kwargs: Any,
    ) -> list[str]:
        """Run more texts through the embeddings and add to the vector store.

        The Links present in the metadata field `links` will be extracted to create
        the `Node` links.

        Eg if nodes `a` and `b` are connected over a hyperlink `https://some-url`, the
        function call would look like:

        .. code-block:: python

            await store.aadd_texts(
                ids=["a", "b"],
                texts=["some text a", "some text b"],
                metadatas=[
                    {
                        "links": [
                            Link.incoming(kind="hyperlink", tag="https://some-url")
                        ]
                    },
                    {
                        "links": [
                            Link.outgoing(kind="hyperlink", tag="https://some-url")
                        ]
                    },
                ],
            )

        Args:
            texts: Iterable of strings to add to the vector store.
            metadatas: Optional list of metadatas associated with the texts.
                The metadata key `links` shall be an iterable of
                :py:class:`~langchain_community.graph_vectorstores.links.Link`.
            ids: Optional list of IDs associated with the texts.
            **kwargs: vector store specific parameters.

        Returns:
            List of ids from adding the texts into the vector store.
        """
        nodes = _texts_to_nodes(texts, metadatas, ids)
        return [_id async for _id in self.aadd_nodes(nodes, **kwargs)]

    def add_documents(
        self,
        documents: Iterable[Document],
        **kwargs: Any,
    ) -> list[str]:
        """Run more documents through the embeddings and add to the vector store.

        The Links present in the document metadata field `links` will be extracted to
        create the `Node` links.

        Eg if nodes `a` and `b` are connected over a hyperlink `https://some-url`, the
        function call would look like:

        .. code-block:: python

            store.add_documents(
                [
                    Document(
                        id="a",
                        page_content="some text a",
                        metadata={
                            "links": [
                                Link.incoming(kind="hyperlink", tag="http://some-url")
                            ]
                        }
                    ),
                    Document(
                        id="b",
                        page_content="some text b",
                        metadata={
                            "links": [
                                Link.outgoing(kind="hyperlink", tag="http://some-url")
                            ]
                        }
                    ),
                ]

            )

        Args:
            documents: Documents to add to the vector store.
                The document's metadata key `links` shall be an iterable of
                :py:class:`~langchain_community.graph_vectorstores.links.Link`.

        Returns:
            List of IDs of the added texts.
        """
        nodes = _documents_to_nodes(documents)
        return list(self.add_nodes(nodes, **kwargs))

    async def aadd_documents(
        self,
        documents: Iterable[Document],
        **kwargs: Any,
    ) -> list[str]:
        """Run more documents through the embeddings and add to the vector store.

        The Links present in the document metadata field `links` will be extracted to
        create the `Node` links.

        Eg if nodes `a` and `b` are connected over a hyperlink `https://some-url`, the
        function call would look like:

        .. code-block:: python

            store.add_documents(
                [
                    Document(
                        id="a",
                        page_content="some text a",
                        metadata={
                            "links": [
                                Link.incoming(kind="hyperlink", tag="http://some-url")
                            ]
                        }
                    ),
                    Document(
                        id="b",
                        page_content="some text b",
                        metadata={
                            "links": [
                                Link.outgoing(kind="hyperlink", tag="http://some-url")
                            ]
                        }
                    ),
                ]

            )

        Args:
            documents: Documents to add to the vector store.
                The document's metadata key `links` shall be an iterable of
                :py:class:`~langchain_community.graph_vectorstores.links.Link`.

        Returns:
            List of IDs of the added texts.
        """
        nodes = _documents_to_nodes(documents)
        return [_id async for _id in self.aadd_nodes(nodes, **kwargs)]

    @abstractmethod
    def traversal_search(
        self,
        query: str,
        *,
        k: int = 4,
        depth: int = 1,
        filter: dict[str, Any] | None = None,  # noqa: A002
        **kwargs: Any,
    ) -> Iterable[Document]:
        """Retrieve documents from traversing this graph store.

        First, `k` nodes are retrieved using a search for each `query` string.
        Then, additional nodes are discovered up to the given `depth` from those
        starting nodes.

        Args:
            query: The query string.
            k: The number of Documents to return from the initial search.
                Defaults to 4. Applies to each of the query strings.
            depth: The maximum depth of edges to traverse. Defaults to 1.
            filter: Optional metadata to filter the results.
            **kwargs: Additional keyword arguments.
        Returns:
            Collection of retrieved documents.
        """

    async def atraversal_search(
        self,
        query: str,
        *,
        k: int = 4,
        depth: int = 1,
        filter: dict[str, Any] | None = None,  # noqa: A002
        **kwargs: Any,
    ) -> AsyncIterable[Document]:
        """Retrieve documents from traversing this graph store.

        First, `k` nodes are retrieved using a search for each `query` string.
        Then, additional nodes are discovered up to the given `depth` from those
        starting nodes.

        Args:
            query: The query string.
            k: The number of Documents to return from the initial search.
                Defaults to 4. Applies to each of the query strings.
            depth: The maximum depth of edges to traverse. Defaults to 1.
            filter: Optional metadata to filter the results.
            **kwargs: Additional keyword arguments.
        Returns:
            Collection of retrieved documents.
        """
        iterator = iter(
            await run_in_executor(
                None,
                self.traversal_search,
                query,
                k=k,
                depth=depth,
                filter=filter,
                **kwargs,
            )
        )
        done = object()
        while True:
            doc = await run_in_executor(None, next, iterator, done)
            if doc is done:
                break
            yield doc  # type: ignore[misc]

    @abstractmethod
    def mmr_traversal_search(
        self,
        query: str,
        *,
        initial_roots: Sequence[str] = (),
        k: int = 4,
        depth: int = 2,
        fetch_k: int = 100,
        adjacent_k: int = 10,
        lambda_mult: float = 0.5,
        score_threshold: float = float("-inf"),
        filter: dict[str, Any] | None = None,  # noqa: A002
        **kwargs: Any,
    ) -> Iterable[Document]:
        """Retrieve documents from this graph store using MMR-traversal.

        This strategy first retrieves the top `fetch_k` results by similarity to
        the question. It then selects the top `k` results based on
        maximum-marginal relevance using the given `lambda_mult`.

        At each step, it considers the (remaining) documents from `fetch_k` as
        well as any documents connected by edges to a selected document
        retrieved based on similarity (a "root").

        Args:
            query: The query string to search for.
            initial_roots: Optional list of document IDs to use for initializing search.
                The top `adjacent_k` nodes adjacent to each initial root will be
                included in the set of initial candidates. To fetch only in the
                neighborhood of these nodes, set `fetch_k = 0`.
            k: Number of Documents to return. Defaults to 4.
            fetch_k: Number of Documents to fetch via similarity.
                Defaults to 100.
            adjacent_k: Number of adjacent Documents to fetch.
                Defaults to 10.
            depth: Maximum depth of a node (number of edges) from a node
                retrieved via similarity. Defaults to 2.
            lambda_mult: Number between 0 and 1 that determines the degree
                of diversity among the results with 0 corresponding to maximum
                diversity and 1 to minimum diversity. Defaults to 0.5.
            score_threshold: Only documents with a score greater than or equal
                this threshold will be chosen. Defaults to negative infinity.
            filter: Optional metadata to filter the results.
            **kwargs: Additional keyword arguments.
        """

    async def ammr_traversal_search(
        self,
        query: str,
        *,
        initial_roots: Sequence[str] = (),
        k: int = 4,
        depth: int = 2,
        fetch_k: int = 100,
        adjacent_k: int = 10,
        lambda_mult: float = 0.5,
        score_threshold: float = float("-inf"),
        filter: dict[str, Any] | None = None,  # noqa: A002
        **kwargs: Any,
    ) -> AsyncIterable[Document]:
        """Retrieve documents from this graph store using MMR-traversal.

        This strategy first retrieves the top `fetch_k` results by similarity to
        the question. It then selects the top `k` results based on
        maximum-marginal relevance using the given `lambda_mult`.

        At each step, it considers the (remaining) documents from `fetch_k` as
        well as any documents connected by edges to a selected document
        retrieved based on similarity (a "root").

        Args:
            query: The query string to search for.
            initial_roots: Optional list of document IDs to use for initializing search.
                The top `adjacent_k` nodes adjacent to each initial root will be
                included in the set of initial candidates. To fetch only in the
                neighborhood of these nodes, set `fetch_k = 0`.
            k: Number of Documents to return. Defaults to 4.
            fetch_k: Number of Documents to fetch via similarity.
                Defaults to 100.
            adjacent_k: Number of adjacent Documents to fetch.
                Defaults to 10.
            depth: Maximum depth of a node (number of edges) from a node
                retrieved via similarity. Defaults to 2.
            lambda_mult: Number between 0 and 1 that determines the degree
                of diversity among the results with 0 corresponding to maximum
                diversity and 1 to minimum diversity. Defaults to 0.5.
            score_threshold: Only documents with a score greater than or equal
                this threshold will be chosen. Defaults to negative infinity.
            filter: Optional metadata to filter the results.
            **kwargs: Additional keyword arguments.
        """
        iterator = iter(
            await run_in_executor(
                None,
                self.mmr_traversal_search,
                query,
                initial_roots=initial_roots,
                k=k,
                fetch_k=fetch_k,
                adjacent_k=adjacent_k,
                depth=depth,
                lambda_mult=lambda_mult,
                score_threshold=score_threshold,
                filter=filter,
                **kwargs,
            )
        )
        done = object()
        while True:
            doc = await run_in_executor(None, next, iterator, done)
            if doc is done:
                break
            yield doc  # type: ignore[misc]

    def similarity_search(
        self, query: str, k: int = 4, **kwargs: Any
    ) -> list[Document]:
        return list(self.traversal_search(query, k=k, depth=0))

    def max_marginal_relevance_search(
        self,
        query: str,
        k: int = 4,
        fetch_k: int = 20,
        lambda_mult: float = 0.5,
        **kwargs: Any,
    ) -> list[Document]:
        if kwargs.get("depth", 0) > 0:
            logger.warning(
                "'mmr' search started with depth > 0. "
                "Maybe you meant to do a 'mmr_traversal' search?"
            )
        return list(
            self.mmr_traversal_search(
                query, k=k, fetch_k=fetch_k, lambda_mult=lambda_mult, depth=0
            )
        )

    async def asimilarity_search(
        self, query: str, k: int = 4, **kwargs: Any
    ) -> list[Document]:
        return [doc async for doc in self.atraversal_search(query, k=k, depth=0)]

    def search(self, query: str, search_type: str, **kwargs: Any) -> list[Document]:
        if search_type == "similarity":
            return self.similarity_search(query, **kwargs)
        elif search_type == "similarity_score_threshold":
            docs_and_similarities = self.similarity_search_with_relevance_scores(
                query, **kwargs
            )
            return [doc for doc, _ in docs_and_similarities]
        elif search_type == "mmr":
            return self.max_marginal_relevance_search(query, **kwargs)
        elif search_type == "traversal":
            return list(self.traversal_search(query, **kwargs))
        elif search_type == "mmr_traversal":
            return list(self.mmr_traversal_search(query, **kwargs))
        else:
            raise ValueError(
                f"search_type of {search_type} not allowed. Expected "
                "search_type to be 'similarity', 'similarity_score_threshold', "
                "'mmr', 'traversal', or 'mmr_traversal'."
            )

    async def asearch(
        self, query: str, search_type: str, **kwargs: Any
    ) -> list[Document]:
        if search_type == "similarity":
            return await self.asimilarity_search(query, **kwargs)
        elif search_type == "similarity_score_threshold":
            docs_and_similarities = await self.asimilarity_search_with_relevance_scores(
                query, **kwargs
            )
            return [doc for doc, _ in docs_and_similarities]
        elif search_type == "mmr":
            return await self.amax_marginal_relevance_search(query, **kwargs)
        elif search_type == "traversal":
            return [doc async for doc in self.atraversal_search(query, **kwargs)]
        elif search_type == "mmr_traversal":
            return [doc async for doc in self.ammr_traversal_search(query, **kwargs)]
        else:
            raise ValueError(
                f"search_type of {search_type} not allowed. Expected "
                "search_type to be 'similarity', 'similarity_score_threshold', "
                "'mmr', 'traversal', or 'mmr_traversal'."
            )

    def as_retriever(self, **kwargs: Any) -> GraphVectorStoreRetriever:
        """Return GraphVectorStoreRetriever initialized from this GraphVectorStore.

        Args:
            **kwargs: Keyword arguments to pass to the search function.
                Can include:

                - search_type (Optional[str]): Defines the type of search that
                  the Retriever should perform.
                  Can be ``traversal`` (default), ``similarity``, ``mmr``,
                   ``mmr_traversal``, or ``similarity_score_threshold``.
                - search_kwargs (Optional[Dict]): Keyword arguments to pass to the
                  search function. Can include things like:

                  - k(int): Amount of documents to return (Default: 4).
                  - depth(int): The maximum depth of edges to traverse (Default: 1).
                    Only applies to search_type: ``traversal`` and ``mmr_traversal``.
                  - score_threshold(float): Minimum relevance threshold
                    for similarity_score_threshold.
                  - fetch_k(int): Amount of documents to pass to MMR algorithm
                    (Default: 20).
                  - lambda_mult(float): Diversity of results returned by MMR;
                    1 for minimum diversity and 0 for maximum. (Default: 0.5).
        Returns:
            Retriever for this GraphVectorStore.

        Examples:

        .. code-block:: python

            # Retrieve documents traversing edges
            docsearch.as_retriever(
                search_type="traversal",
                search_kwargs={'k': 6, 'depth': 2}
            )

            # Retrieve documents with higher diversity
            # Useful if your dataset has many similar documents
            docsearch.as_retriever(
                search_type="mmr_traversal",
                search_kwargs={'k': 6, 'lambda_mult': 0.25, 'depth': 2}
            )

            # Fetch more documents for the MMR algorithm to consider
            # But only return the top 5
            docsearch.as_retriever(
                search_type="mmr_traversal",
                search_kwargs={'k': 5, 'fetch_k': 50, 'depth': 2}
            )

            # Only retrieve documents that have a relevance score
            # Above a certain threshold
            docsearch.as_retriever(
                search_type="similarity_score_threshold",
                search_kwargs={'score_threshold': 0.8}
            )

            # Only get the single most similar document from the dataset
            docsearch.as_retriever(search_kwargs={'k': 1})

        """
        return GraphVectorStoreRetriever(vectorstore=self, **kwargs)


@beta(message="Added in version 0.3.1 of langchain_community. API subject to change.")
class GraphVectorStoreRetriever(VectorStoreRetriever):
    """Retriever for GraphVectorStore.

    A graph vector store retriever is a retriever that uses a graph vector store to
    retrieve documents.
    It is similar to a vector store retriever, except that it uses both vector
    similarity and graph connections to retrieve documents.
    It uses the search methods implemented by a graph vector store, like traversal
    search and MMR traversal search, to query the texts in the graph vector store.

    Example::

        store = CassandraGraphVectorStore(...)
        retriever = store.as_retriever()
        retriever.invoke("What is ...")

    .. seealso::

        :mod:`How to use a graph vector store <langchain_community.graph_vectorstores>`

    How to use a graph vector store as a retriever
    ==============================================

    Creating a retriever from a graph vector store
    ----------------------------------------------

    You can build a retriever from a graph vector store using its
    :meth:`~langchain_community.graph_vectorstores.base.GraphVectorStore.as_retriever`
    method.

    First we instantiate a graph vector store.
    We will use a store backed by Cassandra
    :class:`~langchain_community.graph_vectorstores.cassandra.CassandraGraphVectorStore`
    graph vector store::

        from langchain_community.document_loaders import TextLoader
        from langchain_community.graph_vectorstores import CassandraGraphVectorStore
        from langchain_community.graph_vectorstores.extractors import (
            KeybertLinkExtractor,
            LinkExtractorTransformer,
        )
        from langchain_openai import OpenAIEmbeddings
        from langchain_text_splitters import CharacterTextSplitter

        loader = TextLoader("state_of_the_union.txt")
        documents = loader.load()

        text_splitter = CharacterTextSplitter(chunk_size=1000, chunk_overlap=0)
        texts = text_splitter.split_documents(documents)

        pipeline = LinkExtractorTransformer([KeybertLinkExtractor()])
        pipeline.transform_documents(texts)
        embeddings = OpenAIEmbeddings()
        graph_vectorstore = CassandraGraphVectorStore.from_documents(texts, embeddings)

    We can then instantiate a retriever::

        retriever = graph_vectorstore.as_retriever()

    This creates a retriever (specifically a ``GraphVectorStoreRetriever``), which we
    can use in the usual way::

        docs = retriever.invoke("what did the president say about ketanji brown jackson?")

    Maximum marginal relevance traversal retrieval
    ----------------------------------------------

    By default, the graph vector store retriever uses similarity search, then expands
    the retrieved set by following a fixed number of graph edges.
    If the underlying graph vector store supports maximum marginal relevance traversal,
    you can specify that as the search type.

    MMR-traversal is a retrieval method combining MMR and graph traversal.
    The strategy first retrieves the top fetch_k results by similarity to the question.
    It then iteratively expands the set of fetched documents by following adjacent_k
    graph edges and selects the top k results based on maximum-marginal relevance using
    the given ``lambda_mult``::

        retriever = graph_vectorstore.as_retriever(search_type="mmr_traversal")

    Passing search parameters
    -------------------------

    We can pass parameters to the underlying graph vector store's search methods using
    ``search_kwargs``.

    Specifying graph traversal depth
    ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

    For example, we can set the graph traversal depth to only return documents
    reachable through a given number of graph edges::

        retriever = graph_vectorstore.as_retriever(search_kwargs={"depth": 3})

    Specifying MMR parameters
    ^^^^^^^^^^^^^^^^^^^^^^^^^

    When using search type ``mmr_traversal``, several parameters of the MMR algorithm
    can be configured.

    The ``fetch_k`` parameter determines how many documents are fetched using vector
    similarity and ``adjacent_k`` parameter determines how many documents are fetched
    using graph edges.
    The ``lambda_mult`` parameter controls how the MMR re-ranking weights similarity to
    the query string vs diversity among the retrieved documents as fetched documents
    are selected for the set of ``k`` final results::

        retriever = graph_vectorstore.as_retriever(
            search_type="mmr",
            search_kwargs={"fetch_k": 20, "adjacent_k": 20, "lambda_mult": 0.25},
        )

    Specifying top k
    ^^^^^^^^^^^^^^^^

    We can also limit the number of documents ``k`` returned by the retriever.

    Note that if ``depth`` is greater than zero, the retriever may return more documents
    than is specified by ``k``, since both the original ``k`` documents retrieved using
    vector similarity and any documents connected via graph edges will be returned::

        retriever = graph_vectorstore.as_retriever(search_kwargs={"k": 1})

    Similarity score threshold retrieval
    ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

    For example, we can set a similarity score threshold and only return documents with
    a score above that threshold::

        retriever = graph_vectorstore.as_retriever(search_kwargs={"score_threshold": 0.5})
    """  # noqa: E501

    vectorstore: VectorStore
    """VectorStore to use for retrieval."""
    search_type: str = "traversal"
    """Type of search to perform. Defaults to "traversal"."""
    allowed_search_types: ClassVar[Collection[str]] = (
        "similarity",
        "similarity_score_threshold",
        "mmr",
        "traversal",
        "mmr_traversal",
    )

    @property
    def graph_vectorstore(self) -> GraphVectorStore:
        return cast(GraphVectorStore, self.vectorstore)

    def _get_relevant_documents(
        self, query: str, *, run_manager: CallbackManagerForRetrieverRun, **kwargs: Any
    ) -> list[Document]:
        if self.search_type == "traversal":
            return list(
                self.graph_vectorstore.traversal_search(query, **self.search_kwargs)
            )
        elif self.search_type == "mmr_traversal":
            return list(
                self.graph_vectorstore.mmr_traversal_search(query, **self.search_kwargs)
            )
        else:
            return super()._get_relevant_documents(query, run_manager=run_manager)

    async def _aget_relevant_documents(
        self,
        query: str,
        *,
        run_manager: AsyncCallbackManagerForRetrieverRun,
        **kwargs: Any,
    ) -> list[Document]:
        if self.search_type == "traversal":
            return [
                doc
                async for doc in self.graph_vectorstore.atraversal_search(
                    query, **self.search_kwargs
                )
            ]
        elif self.search_type == "mmr_traversal":
            return [
                doc
                async for doc in self.graph_vectorstore.ammr_traversal_search(
                    query, **self.search_kwargs
                )
            ]
        else:
            return await super()._aget_relevant_documents(
                query, run_manager=run_manager
            )
