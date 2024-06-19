import os
import pickle
import time
from datetime import datetime

import pandas as pd
import numpy as np
import transformers
from transformers import AutoTokenizer, AutoModel
import faiss
from typing import Tuple, List
import logging
from rag_application import constants

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')


def log_creation_time(file_path):
    ctime = os.path.getctime(file_path)
    creation_time = datetime.fromtimestamp(ctime).strftime('%Y-%m-%d %H:%M:%S')
    print(f"File '{file_path}' was created on {creation_time}")


class VectorIndex:
    """
    VectorIndex for creating and querying a FAISS index using BERT embeddings.
    Uses batch processing to avoid loading the entire dataset into memory at once.
    Ensures that the FAISS index is created once and reused throughout the application life of the container.
    """
    _instance = None
    _index = None
    _products_df = None
    _is_index_created = False

    @classmethod
    def get_instance(cls, **kwargs):
        """Static access method to get the singleton instance, enforcing required arguments."""
        logging.info("Entering get_instance method")
        if cls._instance is None:
            logging.info("Instance is None, creating new instance")
            pickle_file = kwargs.get('pickle_file', 'vector_index.pkl')
            products_file = kwargs.get('products_file', '')

            # Check if 'products_file' is a string
            if not isinstance(products_file, str):
                logging.error("'products_file' argument must be a string")
                raise TypeError("'products_file' argument must be a string")

            if os.path.exists(pickle_file):
                logging.info(f"Loading VectorIndex instance from {pickle_file}")
                try:
                    with open(pickle_file, 'rb') as file:
                        cls._instance = pickle.load(file)
                    logging.info("VectorIndex instance loaded from pickle file.")
                except Exception as e:
                    logging.error(f"Failed to load VectorIndex from pickle file: {e}")
                    raise
            else:
                logging.info("Creating new instance of VectorIndex...")
                cls._instance = cls(products_file=products_file)
                try:
                    cls._instance.verify_or_wait_for_file_creation()
                    cls._instance.load_processed_products()
                    cls._instance.create_faiss_index()
                    with open(pickle_file, 'wb') as file:
                        pickle.dump(cls._instance, file)
                    logging.info("VectorIndex instance created and serialized to pickle file.")
                except Exception as e:
                    logging.error(f"Failed to initialize the FAISS index: {str(e)}")
                    raise RuntimeError(f"Error initializing the FAISS index: {str(e)}")
        else:
            logging.info("Using existing instance of VectorIndex")

        return cls._instance

    def __init__(self, products_file=None, batch_size=32):  # m=16
        self.products_df = None
        self.llm = None
        self.products_file = products_file
        self.batch_size = batch_size
        self.embeddings_dict = {}

    def load_processed_products(self):
        """Loads the processed products data with error handling."""
        logging.info("Loading preprocessed products.")
        print("Loading preprocessed products.")

        try:
            self.products_df = pd.read_parquet(self.products_file)
            logging.info("Completed loading preprocessed products.")
        except FileNotFoundError:
            logging.error(f"File {self.products_file} not found.")
        except Exception as e:
            logging.error(f"An error occurred while loading the file: {e}")

    def encode_text_to_embedding(self, texts: List[str]):
        """Encodes a list of texts to BERT embeddings with error handling."""
        logging.info("Encoding text to embedding.")
        print("Encoding text to embedding.")
        embeddings = []
        logging.info("Tokenizing...")
        print("Tokenizing...")
        tokenizer = AutoTokenizer.from_pretrained('bert-base-uncased')

        # Log transformers library version
        logging.info(f"Transformers library version: {transformers.__version__}")
        logging.info("Creating model via AutoModel.from_pretrained('bert-base-uncased')...")
        print("Creating model via AutoModel.from_pretrained('bert-base-uncased')...")
        model = AutoModel.from_pretrained('bert-base-uncased')
        logging.info("Completed creating model via AutoModel.from_pretrained('bert-base-uncased')...")

        total_batches = (len(texts) + self.batch_size - 1)
        for batch in range(0, len(texts), self.batch_size):
            print(f"Encoding text batch {batch} of {total_batches}.")
            logging.info(f"Encoding text batch {batch} of {total_batches}.")
            batch_texts = texts[batch:batch + self.batch_size]
            if not batch_texts:
                continue
            try:
                inputs = tokenizer(batch_texts, return_tensors="pt", padding=True, truncation=True, max_length=512)
                outputs = model(**inputs)
                batch_embeddings = outputs.last_hidden_state[:, 0, :].detach().numpy()

                # Dimensionality check
                expected_dim = 768  # BERT-base embeddings have 768 dimensions
                if batch_embeddings.ndim != 2 or batch_embeddings.shape[1] != expected_dim:
                    raise ValueError(
                        f"Inconsistent embedding dimensions. Expected {expected_dim}, got {batch_embeddings.shape[1]}")

                embeddings.extend(batch_embeddings)
                print(f"Finished encoding text batch {batch} of {total_batches}.")
                logging.info(f"Finished encoding text batch {batch} of {total_batches}.")
            except Exception as e:
                print(f"An error occurred during embedding extraction: {e}")
                logging.error(f"An error occurred during embedding extraction: {e}")
        logging.info("Returning embeddings.")
        print("Returning embeddings.")
        return np.array(embeddings)

    def create_faiss_index(self):
        """Creates an FAISS IVF-HC index for efficient vector similarity search with batch processing."""
        logging.info("Creating an FAISS IVF-HC index for efficient vector similarity search with batch processing.")
        print("Creating an FAISS IVF-HC index for efficient vector similarity search with batch processing.")

        combined_texts = self.products_df['combined_text'].tolist()
        embeddings = self.encode_text_to_embedding(combined_texts)
        # Update embeddings_dict with product_id as key and embedding as value
        for i, product_id in enumerate(self.products_df['product_id']):
            self.embeddings_dict[product_id] = embeddings[i]

        logging.info("Embeddings dictionary updated.")
        print("Embeddings dictionary updated.")

        expected_dim = 768  # BERT base model has 768 dimensions
        if embeddings.ndim != 2 or embeddings.shape[1] != expected_dim:
            msg = f"Inconsistent embedding dimensions. Expected {expected_dim}, got {embeddings.shape[1]}"
            print(msg)
            logging.error(msg)
            raise ValueError(msg)

        d = embeddings.shape[1]  # Dimensionality of the embeddings

        # Create the quantizer and index.
        logging.info("Creating quantizer")
        print("Creating quantizer")
        quantizer = faiss.IndexFlatL2(d)

        # Each vector is split into m subvectors/subquantizers.
        m = 8

        # There's a trade-off between memory efficiency and search accuracy.
        # Using more bits per subquantizer generally leads to more accurate searches
        # but requires more memory.
        bits = 8  # Reduced bits to ensure it fits within the limitations

        # Calculate a suitable nlist value
        num_points = embeddings.shape[0]
        nlist = max(1, int(np.sqrt(num_points)))  # Ensure nlist is at least 1

        # Ensure nlist does not exceed the number of points
        if nlist > num_points:
            nlist = num_points

        # IVFPQ chosen for improved speed
        self._index = faiss.IndexIVFPQ(quantizer, d, nlist, m, bits)

        # Ensure embeddings is a numpy array
        embeddings_np = np.array(embeddings)

        # Generate numeric IDs for FAISS
        numeric_ids = np.arange(len(self.products_df)).astype(np.int64)

        # Train the index and add embeddings
        logging.info(f"Checking if trained: {self._index.is_trained}")
        print(f"Checking if trained: {self._index.is_trained}")
        if not self._index.is_trained:
            logging.info("Training...")
            print("Training...")
            self._index.train(embeddings_np)

        logging.info(f"Is trained: {self._index.is_trained}")
        print(f"Is trained: {self._index.is_trained}")

        logging.info("Embedding...")
        print("Embedding...")
        self._index.add_with_ids(embeddings_np, numeric_ids)
        logging.info(f"Embedding completed. nTotal = {self._index.ntotal}")
        print(f"Embedding completed. nTotal = {self._index.ntotal}")
        self._is_index_created = True

    def search_index(self, query: str, k: int = 10) -> Tuple[np.ndarray, np.ndarray]:
        """
        Searches for the k nearest neighbors of the query.
        :param query: The text query to search for.
        :param k: Number of nearest neighbors to return.
        :return: A tuple containing distances and indices of the nearest neighbors.
        """
        logging.info("Searching for the k nearest neighbors of the query.")
        print("Searching for the k nearest neighbors of the query.")

        # Check if the index is initialized
        if self._index is None:
            logging.error("Index is not initialized.")
            raise RuntimeError("Index is not initialized.")

        # Check if the query is empty and raise a ValueError if it is
        if not query.strip():
            logging.error("Query string cannot be empty.")
            raise ValueError("Query string cannot be empty.")

        # Check if k is an integer > 0
        if not isinstance(k, int) or k <= 0:
            logging.error("Search radius must be an integer greater than 0.")
            raise TypeError("Search radius must be an integer greater than 0.")

        # Initialize variables to store results
        faiss_distances = None
        faiss_result_indices = None
        direct_search_embedding = None

        # Perform Faiss similarity search
        try:
            # Convert the query string into a numerical vector
            query_vector = self.encode_text_to_embedding([query])

            # Ensure the query vector has the correct shape for FAISS search
            if len(query_vector.shape) == 1:
                query_vector = np.expand_dims(query_vector, axis=0)

            # Search the FAISS index
            logging.info("Searching the FAISS index.")
            print("Searching the FAISS index.")

            # Setting the number of Voronoi cells that are visited during the search.
            self._index.nprobe = 6

            # Search
            faiss_distances, faiss_result_indices = self._index.search(query_vector, k)

            logging.info(f"Returning distance: {str(faiss_distances.tolist())}")
            print(f"Returning distance: {str(faiss_distances.tolist())}")

            logging.info(f"Returning result_index: {str(faiss_result_indices.tolist())}")
            print(f"Returning result_index: {str(faiss_result_indices.tolist())}")

        except Exception as e:
            logging.error(f"Error during FAISS search: {e}")
            print(f"Error during FAISS search: {e}")

        # Combine results from FAISS search and direct search
        combined_distances = []
        combined_indices = []

        # Perform direct search by product ID
        try:
            product_id = int(query)
            logging.info(f"Searching for product_id: {product_id}")
            print(f"Searching for product_id: {product_id}")
            direct_search_embedding = self.search_by_product_id(product_id)
            # Add direct search result if available
            if direct_search_embedding is not None:
                combined_distances.append(0.0)  # Distance 0 == direct match
                combined_indices.append(-1)  # -1 == identifier for direct match

                # Add direct search embedding to embeddings_dict if not already present
                if product_id is not None and product_id not in self.embeddings_dict:
                    self.embeddings_dict[product_id] = direct_search_embedding
        except ValueError:
            logging.warning(f"Query '{query}' cannot be interpreted as a product ID.")

        # Add FAISS search results if available
        if faiss_distances is not None and faiss_result_indices is not None:
            combined_distances.extend(faiss_distances[0])
            combined_indices.extend(faiss_result_indices[0])

        # Convert lists to numpy arrays
        combined_distances = np.array(combined_distances)
        combined_indices = np.array(combined_indices)

        return combined_distances, combined_indices

    @staticmethod
    def find_changed_products(old_descriptions, new_descriptions):
        """
        Identifies products whose descriptions have changed.

        Parameters:
        - old_descriptions (dict): Mapping of product IDs to their old descriptions.
        - new_descriptions (dict): Mapping of product IDs to their new descriptions.

        Returns:
        - set: Set of product IDs whose descriptions have changed.
        """
        logging.info("Searching for changed products.")
        print("Searching for changed products.")
        changed_products = set()
        for product_id, new_desc in new_descriptions.items():
            old_desc = old_descriptions.get(product_id)
            if old_desc != new_desc:
                changed_products.add(product_id)
        try:
            logging.info(f"Returning changed_products: {str(changed_products)}")
            print(f"Returning changed_products: {str(changed_products)}")
        except Exception as e:
            logging.error("Error: Unable to convert changed_products data to string")
            print("Error: Unable to convert changed_products data to string")

        return changed_products

    def update_product_descriptions(self, updates):
        """
        Batch updates the descriptions of multiple products and regenerates their embeddings.

        Parameters:
        - updates (dict): Mapping of product IDs to their new descriptions.

        Raises:
        - KeyError: If a product ID in updates is not found in the DataFrame.
        """
        logging.info(
            "Making batch updates for the descriptions of multiple products and regenerating their embeddings.")
        print("Making batch updates for the descriptions of multiple products and regenerating their embeddings.")

        # Find products whose descriptions have changed
        changed_products = self.find_changed_products(
            {pid: row['product_description'] for pid, row in self.products_df.iterrows()}, updates)
        logging.info(f"Changed products list: {str(list(changed_products))}")
        print(f"Changed products list: {str(list(changed_products))}")

        # Update descriptions in the DataFrame
        for product_id, new_description in updates.items():
            if product_id not in self.products_df['product_id'].values:
                raise KeyError(f"Product ID {product_id} not found in the DataFrame.")
            self.products_df.loc[self.products_df['product_id'] == product_id, 'product_description'] = new_description
            self.products_df.loc[self.products_df['product_id'] == product_id, 'combined_text'] = \
                f"{self.products_df.loc[self.products_df['product_id'] == product_id, 'product_title'].values[0]} {new_description}"

        if changed_products:
            self.update_embeddings_for_changed_products(list(changed_products))

    def update_embeddings_for_changed_products(self, changed_product_ids: List[str]):
        """Re-encodes and re-adds embeddings for products whose descriptions were changed."""
        logging.info("Re-encoding and re-adding embeddings for products whose descriptions were changed.")
        print("Re-encoding and re-adding embeddings for products whose descriptions were changed.")

        for product_id in changed_product_ids:
            try:
                combined_text = f"{self.products_df.at[product_id, 'product_title']} {self.products_df.at[product_id, 'product_description']}"
                new_embedding = self.encode_text_to_embedding([combined_text])[0]
                self._index.add_with_ids(new_embedding.reshape(1, -1), np.array([product_id]))
                self.embeddings_dict[product_id] = new_embedding
                print(f"Product: {product_id}... Embedding updated.")
            except KeyError as e:
                logging.error(f"Product ID {product_id} not found in the DataFrame.")
                raise RuntimeError(f"Product ID {product_id} not found in the DataFrame.")
            except Exception as e:
                logging.error(f"Error updating embeddings for product ID {product_id}: {e}")
                raise RuntimeError(f"Error updating embeddings for product ID {product_id}: {e}")

        logging.info("Completed embedding updates.")
        print("Completed embedding updates.")

    def remove_product_by_id(self, product_id):
        """Removes a product by ID from the index and the underlying data store."""
        logging.info(f"Removing product by ID {product_id} from the index and the underlying data store.")
        print(f"Removing product by ID {product_id} from the index and the underlying data store.")

        if product_id not in self.products_df['product_id'].values:
            raise ValueError(f"Product ID {product_id} not found.")

        # Remove the product by dropping the row with the given index label
        self.products_df = self.products_df[self.products_df['product_id'] != product_id]
        logging.info(f"Product {product_id} removed from DataFrame and index.")
        print(f"Product {product_id} removed from DataFrame and index.")

    def get_all_product_ids(self):
        """Returns all unique product IDs from the products_df DataFrame."""
        logging.info("Returning all unique product IDs from the products_df DataFrame...")
        print("Returning all unique product IDs from the products_df DataFrame...")
        return self.products_df['product_id'].unique().tolist()

    def get_embedding(self, product_id):
        """Fetches the embedding for a given product ID."""
        logging.info(f"Fetching the embedding for {product_id}.")
        print(f"Fetching the embedding for {product_id}.")
        embedding = self.embeddings_dict.get(product_id)
        if embedding is None:
            logging.error("Embedding not found")
            print("Embedding not found")
            raise RuntimeError("Embedding not found")
        logging.info("Returning embedding")
        print("Returning embedding")
        return embedding

    def get_first_10_vectors(self):
        """Returns the first 10 vectors in the index dataframe. Used for testing."""
        return self.products_df.head(10)

    def search_and_generate_response(self, refined_query: str, llm, k: int = 15) -> str:
        # Search the FAISS index with the refined query
        logging.info(f"Searching the index for: {refined_query}")
        distances, relevant_product_indices = self.search_index(refined_query, k=k)

        # Extract the product information based on the returned indices
        product_info_list = []
        for index in relevant_product_indices:
                try:
                    product_info = (
                        f"ID: {index}, "
                        f"Name: {self.products_df.iloc[index]['product_title']}, "
                        f"Description: {self.products_df.iloc[index]['product_description']}, "
                        f"Key Facts: {self.products_df.iloc[index]['product_bullet_point']}, "
                        f"Brand: {self.products_df.iloc[index]['product_brand']}, "
                        f"Color: {self.products_df.iloc[index]['product_color']}, "
                        f"Location: {self.products_df.iloc[index]['product_locale']}"
                    )
                    product_info_list.append(product_info)
                except KeyError:
                    logging.warning(f"Product ID {index} not found in the DataFrame.")

        # Join the product information into a single string
        product_info_str = ", ".join(product_info_list)
        logging.info(f"From search_and_generate_response returning: {product_info_str}")

        return product_info_str

    @classmethod
    def verify_or_wait_for_file_creation(cls):
        logging.info("Waiting for file generation.")

        # Define the path to the file
        base_dir = os.path.dirname(os.path.abspath(__file__))
        data_dir = os.path.join(base_dir, 'shopping_queries_dataset')
        products_file = os.path.join(data_dir, 'processed_products.parquet')

        # Parameters for retrying
        max_retries = 10
        wait_time = 5  # seconds

        for attempt in range(max_retries):
            if os.path.exists(products_file):
                logging.info(f"File {products_file} found.")
                break
            else:
                logging.warning(f"File {products_file} not found. Retrying in {wait_time} seconds...")
                time.sleep(wait_time)
        else:
            logging.error(f"File {products_file} not found after {max_retries * wait_time} seconds.")
            raise FileNotFoundError(f"File {products_file} not found after {max_retries * wait_time} seconds.")

    def search_by_product_id(self, product_id):
        """Searches for an embedding by product ID."""
        logging.info(f"Searching for embedding by product ID: {product_id}")
        print(f"Searching for embedding by product ID: {product_id}")

        try:
            embedding = self.embeddings_dict[product_id]
            logging.info("Embedding found.")
            print("Embedding found.")
            # Convert embedding to numpy array if needed
            embedding_np = np.array(embedding, dtype=np.float32)  # Assuming embedding is already a numpy array

            # Perform a search in the FAISS index to find the nearest neighbor
            D, I = self._index.search(np.expand_dims(embedding_np, axis=0), 1)
            if len(I) > 0 and I[0][0] != -1:  # Check if a valid index was found
                index = int(I[0][0])  # Extract the index of the nearest neighbor
                logging.info(f"Nearest neighbor index: {index}")
                print(f"Nearest neighbor index: {index}")
                return embedding, index
            else:
                logging.error("No valid nearest neighbor found in the FAISS index.")
                print("No valid nearest neighbor found in the FAISS index.")
                return embedding, None

        except KeyError:
            logging.error(f"Embedding not found for product ID: {product_id}")
            print(f"Embedding not found for product ID: {product_id}")
            return None, None


if __name__ == "__main__":
    try:
        VectorIndex.get_instance()
    except Exception as e:
        logging.error(f"Error creating the FAISS index: {e}")
        raise RuntimeError(f"Error creating the FAISS index: {e}")
    logging.info("FAISS index created successfully.")
    print("FAISS index created successfully.")
