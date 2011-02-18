from numpy import *
from scipy.cluster import vq
import time

# Pseudo-code for hierarchical clustering

def kmnb(s,k):
    assert s >= 1
    nb = k
    while s > 1:
        nb = nb + k**s
        s -= 1
    return nb

def dist(m, v):
    return sum((m-v)**2, axis=1)

def gaussian(X, u, E):
    t = X - u 
    return exp(-0.5*sum(dot(t, E) * t, axis=1))

# Hierarchical clustering
# 
# Inputs
# dataset : n x d matrix with n examples with d features
# max_step: number of recursion steps to perform
# k       : number of clusters at each step
# fullcov :  True will use the covariance
#            None will use the variance along each dimension independently (faster)
# verbose :  True prints status messages as training progresses
#
# Output
#         : 3-uple 
#          nb x d matrix of clusters centroids,
#          list of nb covariance/variance matrices
#          array of nb prior probabilities
#          with nb = (k**1 + k**2 + ... + k**step)
def hc(dataset, max_step=5, k=2, fullcov=True, verbose=False):
    # Nb of clusters to find 
    nb = kmnb(max_step, k)
    # Nb of features
    d = dataset.shape[1]
    # Cluster centroids
    means = zeros((nb, d))
    # Variance
    vars  = [ None for i in range(nb) ]
    # Prior probabilities
    priors   = zeros((nb,))


    nb_done = [0]
    def progress(k): 
        nb_done[0] += k
        if verbose: 
            print "Computed %i/%i clusters"%(nb_done[0], nb)


    def helper(dataset, step, base_idx, offset, prior):
        cs = kmeans(dataset, k)
        p = partition(dataset, cs)

        index = base_idx + k*offset
        means[index:index+k,:] = cs

        progress(k)

        for i in range(k):
            ix = (p == i)
            s = sum(ix)

            priors[index+i] = (prior*(s) / p.shape[0])
            if fullcov: 
                if s > 1:
                    vars[index+i] = cov(dataset[ix], rowvar=0)
                else:
                    vars[index+i] = zeros((d,d))
            else: 
                vars[index+i] = var(dataset[ix], axis=0)

            if step < max_step:
                helper(dataset[ix], step+1, base_idx + k**step, k*offset + i, priors[index+i])

    helper(dataset, 1, 0, 0, 1.0)
    return (means, vars, priors)

# K-mean                
#
# Inputs:
# dataset: n x d matrix with n examples with d features
# k      : integer      number of clusters 
#
# Ouput:
#        :k x d matrix that contains the centroids of each cluster
def kmeans(dataset, k):
    # TODO: Do we need whitening?
    return vq.kmeans(dataset, k)[0]

# Partition the data according to the nearest centroid
#    
# Inputs
# dataset: n x d matrix with n examples with d features
# means  : c x d matrix that contains the centroids of each cluster
#
# Output
#        : n x 1 matrix that contains the index to the nearest cluster 
#          to the nth matrix
def partition(dataset, means):
    n = dataset.shape[0]
    c = means.shape[0]
    p = zeros((n,c))

    for i in range(c):
        p[:,i] = dist(dataset, means[i,:]) 
    return argmin(p, axis=1)

# Compute P(x|C)*P(C) (posterior). If priors is not given,
# P(C) == 1. Normalization by P(x) is optional.
# 
# For speed reasons, the normalization factor of the gaussian
# is ignored.
#
# Inputs:
# dataset: n x d matrix with n examples with d features
# means  : c x d matrix that contains the centroids of each cluster
# vars   : c-tuple of (d x d) covariance matrices of each cluster
#                  or (d x 1) variance vectors
# priors : c x 1 matrix of prior probabilities for each cluster, optional
# k      : int   if 0, no normalization is done, otherwise normalize as if
#                hc was computed with k clusters at each step
# verbose :  True prints status messages
#
# Output:
#        : n x c matrix that contains the probabilities
def probs(dataset, means, vars, priors=None, k=0, verbose=False):
    eps = finfo(float).eps
    Z = identity(vars[0].shape[0]) * (1/eps)
   
    # Make such that a variance of 0 will give a likelihood
    # of 1 for an example directly on the mean and 0 otherwise
    def initCov(v):
        if linalg.det(v) == 0.:
            return Z
        else:
            return linalg.inv(v)

    if len(vars[0].shape) < 2 or vars[0].shape[0] != vars[0].shape[1]:
        vars = [ initCov(diag(var)) for var in vars ]
    else:
        vars = [ initCov(var) for var in vars ]

    n = dataset.shape[0]
    c = means.shape[0]
    ps = zeros((n,c))

    for i in xrange(c):
        if priors != None:
            ps[:,i] = gaussian(dataset, means[i,:], vars[i]) * priors[i]
        else:
            ps[:,i] = gaussian(dataset, means[i,:], vars[i])

        if verbose:
            print "Computed %i/%i probabilities"%(i+1,c)

    # Normalize (Divide all computed posteriors 
    # by the sum of posteriors at the same level)
    if k != 0:
        if verbose:
            print "Normalizing"

        step = 1
        i = 0
        j = k

        while j <= c:
            ps[:,i:j] =  (ps[:,i:j].T / sum(ps[:,i:j], axis=1)).T

            step += 1
            i = j
            j += k**step

    return ps

def test_terry():
    dataset = "terry"
    save_dir_submission = "./"

    data = load("/data/lisa/exp/dauphiya/stackedterry/best_layer0/terry_valid.npy")

    print "Computing clusters"
    start = time.time()    
    (means, vars, priors) = hc(data, 5, 2, verbose=True)
    end = time.time()
    print "Clusters computed in %i s"%(end-start)

    print "Computing probabilities"
    start = time.time()
    valid_rep1 = probs(data, means, vars, priors, k=2, verbose=True)
    end = time.time()
    print "Probabilities computed in %i s"%(end-start)

    valid_rep2 = numpy.dot(valid_rep1,valid_rep1.T)

    # write it in a .txt file
    valid_rep1 = numpy.floor((valid_rep1 / valid_rep1.max())*999)
    valid_rep2 = numpy.floor((valid_rep2 / valid_rep2.max())*999)

    val1 = open(os.path.join(save_dir_submission, dataset + '_dl_valid.prepro'),'w')
    val2 = open(os.path.join(save_dir_submission, dataset + '_sdl_valid.prepro'),'w')

    vtxt1, vtxt2 = '', ''

    for i in range(valid_rep1.shape[0]):
        for j in range(valid_rep1.shape[0]):
            vtxt2 += '%s '%int(valid_rep2[i,j])
        for j in range(valid_rep1.shape[1]):
            vtxt1 += '%s '%int(valid_rep1[i,j])
        vtxt1 += '\n'
        vtxt2 += '\n'
    del valid_rep1, valid_rep2

    val1.write(vtxt1)
    val2.write(vtxt2)
    val1.close()
    val2.close()

    print >> sys.stderr, "... done creating files"

    os.system('zip %s %s %s'%(os.path.join(save_dir_submission, dataset+'_dl.zip'),
        os.path.join(save_dir_submission, dataset+'_dl_valid.prepro'),
        os.path.join(save_dir_submission, dataset+'_dl_final.prepro')))
    os.system('zip %s %s %s'%(os.path.join(save_dir_submission, dataset+'_sdl.zip'),
        os.path.join(save_dir_submission, dataset+'_sdl_valid.prepro'),
        os.path.join(save_dir_submission, dataset+'_sdl_final.prepro')))

    print >> sys.stderr, "... files compressed"

    os.system('rm %s %s %s %s'%(
        os.path.join(save_dir_submission, dataset+'_dl_valid.prepro'),
        os.path.join(save_dir_submission, dataset+'_dl_final.prepro'),
        os.path.join(save_dir_submission, dataset+'_sdl_valid.prepro'),
        os.path.join(save_dir_submission, dataset+ '_sdl_final.prepro')))

    print >> sys.stderr, "... useless files deleted"


if __name__ == "__main__":
    #dataset = array([[-4, 2],[-3, 2],[-4, 1],[-3, 1],[-4,-1],[-3,-1],[-4,-2],[-3,-2],\
    #                 [ 3, 2],[ 4, 2],[ 3, 1],[ 4, 1],[ 3,-1],[ 4,-2],[ 3,-2],[ 4,-1]], dtype='float')
    #dataset = random.rand(4096,1000)
   
    #print "Computing clusters"
    #start = time.time()    
    #(means, vars, priors) = hc(dataset, 3, 2, verbose=True)
    #end = time.time()
    #print "Clusters computed in %i s"%(end-start)

    #print "Computing probabilities"
    #start = time.time()
    #ps = probs(dataset, means, vars, priors, k=2, verbose=True)
    #end = time.time()
    #print "Probabilities computed in %i s"%(end-start)
    test_terry()
    

        

