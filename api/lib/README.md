# libgomp.so.1 (pour l'API en ligne sur Vercel)

LightGBM a besoin de la bibliothèque OpenMP `libgomp.so.1`, absente des machines de Vercel
(`OSError: libgomp.so.1: cannot open shared object file`). Dans Docker, le Dockerfile
l'installe avec `apt-get install libgomp1` ; ici, `api/index.py` charge cette copie avant LightGBM.

Origine : la copie de libgomp livrée dans le wheel officiel scikit-learn 1.9.1
(manylinux_2_28, `scikit_learn.libs/libgomp-e985bcbb.so.1.0.0`, compatible glibc ≥ 2.17),
dont seul le nom interne (SONAME) a été remis à `libgomp.so.1` avec `patchelf --set-soname`.
Bibliothèque de GCC, sous licence GPLv3 avec l'exception « GCC Runtime Library ».
