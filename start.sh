#!/bin/bash
# todo: removed only for local testing
cd /app
# Ensure Conda environment is properly initialized
source /opt/conda/etc/profile.d/conda.sh

# Activate the Conda environment
conda activate ragEnv

source /opt/anaconda3/envs/ragEnv/python

# Verify the Conda environment
echo "Active Conda environment:"
conda env list
echo "Python path:"
which python
# Print Python version
echo "*********************************************************Python version:"
python --version

# Add conda-forge channel
conda config --add channels conda-forge
conda config --set pip_interop_enabled True

# Install required packages
echo "Installing conda packages..."
conda install -c pytorch faiss-cpu=1.7.4 mkl=2021 blas=1.0=mkl

conda install -y langchain==0.1.20
conda install -y langchain-openai==0.0.8
conda install -y langsmith==0.1.63
conda install -y streamlit==1.35.0
conda install -y -c pytorch pytorch==2.2.2 torchvision torchaudio -c defaults
conda install -y -c conda-forge transformers==4.41.1
conda install -c intel mkl
#conda install -y pytest==8.2.1  <<< testing pkg
#conda install -y Faker==25.2.0  <<< testing pkg

# Print Python version
echo "*********************************************************Python version:"
python --version

# Print faiss-cpu version
echo "*********************************************************faiss-cpu version:"
python -c "import faiss; print(faiss.__version__)"

# Run the preprocessing script
python rag_application/modules/preprocess_data.py || { echo "Preprocessing failed"; exit 1; }


# Start Streamlit
exec streamlit run rag_application/modules/user_interface.py --server.port=8505 --server.address=0.0.0.0 || { echo "Streamlit failed to start"; exit 1; }
