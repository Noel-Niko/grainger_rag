import pandas as pd
import numpy as np
from transformers import AutoTokenizer, AutoModel
import faiss
from typing import Tuple, List


class VectorIndex:
    """
    VectorIndex for creating and querying a FAISS index using BERT embeddings.
    Uses batch processing to avoid loading the entire dataset into memory at once.
    Ensures that the FAISS index is created once and reused throughout the application life of the container.
    """
    _instance = None
    _index = None
    _products_df = None

    @classmethod
    def getInstance(cls):
        """Static access method to get the singleton instance."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self, products_file: str, nlist: int = 100, m: int = 16, batch_size: int = 32):
        self.products_file = products_file
        self.nlist = nlist
        self.m = m
        self.batch_size = batch_size
        self.embeddings_dict = {}

    def load_processed_products(self):
        """Loads the processed products data with error handling."""
        try:
            self.products_df = pd.read_parquet(self.products_file)
        except FileNotFoundError:
            print(f"File {self.products_file} not found.")
        except Exception as e:
            print(f"An error occurred while loading the file: {e}")

    def encode_text_to_embedding(self, texts: List[str]):
        """Encodes a list of texts to BERT embeddings with error handling."""
        embeddings = []
        tokenizer = AutoTokenizer.from_pretrained('bert-base-uncased')
        model = AutoModel.from_pretrained('bert-base-uncased')

        for batch in range(0, len(texts), self.batch_size):
            batch_texts = texts[batch:batch + self.batch_size]
            if not batch_texts:
                continue
            try:
                inputs = tokenizer(batch_texts, return_tensors="pt", padding=True, truncation=True, max_length=512)
                outputs = model(**inputs)
                batch_embeddings = outputs.last_hidden_state[:, 0, :].detach().numpy()
                embeddings.extend(batch_embeddings)
            except Exception as e:
                print(f"An error occurred during embedding extraction: {e}")

        return np.array(embeddings)

    def create_faiss_index(self):
        """Creates an FAISS IVF-HC index for efficient vector similarity search with batch processing."""
        combined_texts = self.products_df['combined_text'].tolist()
        embeddings = self.encode_text_to_embedding(combined_texts)
        expected_dim = 768  # Example: BERT base model has 768 dimensions
        if embeddings.ndim != 2 or embeddings.shape[1] != expected_dim:
            raise ValueError(
                f"Inconsistent embedding dimensions. Expected {expected_dim}, got {embeddings.shape[1]}")

        d = embeddings.shape[1]  # Dimensionality of the embeddings

        # Create the quantizer and index
        quantizer = faiss.IndexFlatL2(d)
        self.index = faiss.IndexIVFFlat(quantizer, d, self.nlist)

        # Ensure embeddings is a numpy array
        embeddings_np = np.array(embeddings)

        # Train the index and add embeddings
        self.index.train(embeddings_np)
        self.index.add(embeddings_np)

    def search_index(self, query: str, k: int = 10) -> Tuple[np.ndarray, np.ndarray]:
        """
        Searches for the k nearest neighbors of the query.

        :param query: The text query to search for.
        :param k: Number of nearest neighbors to return.
        :return: A tuple containing distances and indices of the nearest neighbors.
        """
        # Check if the index is initialized
        if self._index is None:
            raise RuntimeError("Index is not initialized.")
        # Check if the query is empty and raise a ValueError if it is
        if not query.strip():
            raise ValueError("Query string cannot be empty.")
        # Check if k is an integer > 0
        if not isinstance(k, int) or k <= 0:
            raise TypeError("Search radius must be an integer greater than 0.")

        # Convert the query string into a numerical vector
        query_vector = self.encode_text_to_embedding([query])

        # Ensure the query vector has the correct shape for FAISS search
        query_vector = np.expand_dims(query_vector, axis=0)

        # Search the FAISS index
        distance, result_index = self.index.search(query_vector[0], k)

        return distance, result_index

    def find_changed_products(self, old_descriptions, new_descriptions):
        """
        Identifies products whose descriptions have changed.

        Parameters:
        - old_descriptions (dict): Mapping of product IDs to their old descriptions.
        - new_descriptions (dict): Mapping of product IDs to their new descriptions.

        Returns:
        - set: Set of product IDs whose descriptions have changed.
        """
        changed_products = set()
        for product_id, new_desc in new_descriptions.items():
            old_desc = old_descriptions.get(product_id)
            if old_desc != new_desc:
                changed_products.add(product_id)
        return changed_products

    def update_product_descriptions(self, updates):
        """
        Batch updates the descriptions of multiple products and regenerates their embeddings.

        Parameters:
        - updates (dict): Mapping of product IDs to their new descriptions.

        Raises:
        - KeyError: If a product ID in updates is not found in the DataFrame.
        """
        # Find products whose descriptions have changed
        changed_products = self.find_changed_products(
            {pid: row['product_description'] for pid, row in self.products_df.iterrows()}, updates)

        # Update descriptions in the DataFrame
        for product_id, new_description in updates.items():
            if product_id not in self.products_df['product_id'].values:
                raise KeyError(f"Product ID {product_id} not found in the DataFrame.")
            product_index = self.products_df[self.products_df['product_id'] == product_id].index[0]
            self.products_df.at[product_index, 'product_description'] = new_description
            self.products_df.at[
                product_index, 'combined_text'] = f"{self.products_df.at[product_index, 'product_title']} {new_description}"

        # Regenerate embeddings only for changed products
        if changed_products:
            self.update_embeddings_for_changed_products(list(changed_products))

    def update_embeddings_for_changed_products(self, changed_product_ids: List[str]):
        """Re-encodes and re-adds embeddings for products whose descriptions were changed."""
        for product_id in changed_product_ids:
            try:
                product_index = self.products_df[self.products_df['product_id'] == product_id].index[0]
                combined_text = f"{self.products_df.at[product_index, 'product_title']} {self.products_df.at[product_index, 'product_description']}"
                new_embedding = self.encode_text_to_embedding([combined_text])[0]
                self.index.add_with_ids(new_embedding.reshape(1, -1), np.array([product_id]))
                self.embeddings_dict[product_id] = new_embedding
            except Exception as e:
                print(f"Error updating embeddings for product ID {product_id}: {e}")

    def remove_product_by_id(self, product_id: str):
        """Removes a product by ID from the index and the underlying data store."""
        if product_id not in self.products_df['product_id'].values:
            raise RuntimeError("product_id not found.")

        product_index = self.products_df[self.products_df['product_id'] == product_id].index[0]
        self.products_df.drop(product_index, inplace=True)
        self.create_faiss_index()  # Re-create the index after removing the product

    def get_all_product_ids(self):
        """Returns all unique product IDs from the products_df DataFrame."""
        return self.products_df['product_id'].unique().tolist()

    def get_embedding(self, product_id):
        """Fetches the embedding for a given product ID."""
        embedding = self.embeddings_dict.get(product_id)
        if embedding is None:
            raise RuntimeError("Embedding not found")

        return embedding

    def get_first_10_vectors(self):
        """Returns the first 10 vectors in the index dataframe. Used for testing."""
        return self.products_df.head(10)


if __name__ == "__main__":
    products_file = 'rag_application/shopping_queries_dataset/processed_products.parquet'
    vector_index = VectorIndex(products_file, batch_size=32)
    vector_index.load_processed_products()
    vector_index.create_faiss_index()
    print("FAISS index created successfully.")
